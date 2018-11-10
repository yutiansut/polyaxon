import logging
import mimetypes
import os

from wsgiref.util import FileWrapper

from hestia.bool_utils import to_bool
from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.generics import (
    CreateAPIView,
    ListAPIView,
    RetrieveAPIView,
    RetrieveUpdateAPIView,
    RetrieveUpdateDestroyAPIView,
    get_object_or_404
)
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.settings import api_settings

from django.http import StreamingHttpResponse

import auditor

from api.code_reference.serializers import CodeReferenceSerializer
from api.experiments import queries
from api.experiments.serializers import (
    BookmarkedExperimentSerializer,
    ExperimentChartViewSerializer,
    ExperimentCreateSerializer,
    ExperimentDeclarationsSerializer,
    ExperimentDetailSerializer,
    ExperimentJobDetailSerializer,
    ExperimentJobSerializer,
    ExperimentJobStatusSerializer,
    ExperimentLastMetricSerializer,
    ExperimentMetricSerializer,
    ExperimentSerializer,
    ExperimentStatusSerializer
)
from api.filters import OrderingFilter, QueryFilter
from api.paginator import LargeLimitOffsetPagination
from api.utils.views.auditor_mixin import AuditorMixinView
from api.utils.views.list_create import ListCreateAPIView
from api.utils.views.post import PostAPIView
from api.utils.views.protected import ProtectedView
from constants.experiments import ExperimentLifeCycle
from db.models.experiment_groups import ExperimentGroup
from db.models.experiment_jobs import ExperimentJob, ExperimentJobStatus
from db.models.experiments import (
    Experiment,
    ExperimentChartView,
    ExperimentMetric,
    ExperimentStatus
)
from db.models.projects import Project
from db.models.tokens import Token
from db.redis.ephemeral_tokens import RedisEphemeralTokens
from db.redis.heartbeat import RedisHeartBeat
from db.redis.tll import RedisTTL
from event_manager.events.chart_view import CHART_VIEW_CREATED, CHART_VIEW_DELETED
from event_manager.events.experiment import (
    EXPERIMENT_COPIED_TRIGGERED,
    EXPERIMENT_CREATED,
    EXPERIMENT_DELETED_TRIGGERED,
    EXPERIMENT_JOBS_VIEWED,
    EXPERIMENT_LOGS_VIEWED,
    EXPERIMENT_METRICS_VIEWED,
    EXPERIMENT_OUTPUTS_DOWNLOADED,
    EXPERIMENT_RESTARTED_TRIGGERED,
    EXPERIMENT_RESUMED_TRIGGERED,
    EXPERIMENT_STATUSES_VIEWED,
    EXPERIMENT_STOPPED_TRIGGERED,
    EXPERIMENT_UPDATED,
    EXPERIMENT_VIEWED
)
from event_manager.events.experiment_group import EXPERIMENT_GROUP_EXPERIMENTS_VIEWED
from event_manager.events.experiment_job import (
    EXPERIMENT_JOB_STATUSES_VIEWED,
    EXPERIMENT_JOB_VIEWED
)
from event_manager.events.project import PROJECT_EXPERIMENTS_VIEWED
from libs.archive import archive_experiment_outputs, archive_outputs_file
from libs.paths.exceptions import VolumeNotFoundError
from libs.paths.experiments import get_experiment_logs_path, get_experiment_outputs_path
from scopes.authentication.ephemeral import EphemeralAuthentication
from scopes.authentication.internal import InternalAuthentication
from scopes.permissions.ephemeral import IsEphemeral
from scopes.permissions.internal import IsAuthenticatedOrInternal
from scopes.permissions.projects import IsProjectOwnerOrPublicReadOnly, get_permissible_project
from libs.spec_validation import validate_experiment_spec_config
from libs.stores import get_outputs_store
from polyaxon.celery_api import celery_app
from polyaxon.settings import LogsCeleryTasks, SchedulerCeleryTasks

_logger = logging.getLogger("polyaxon.views.experiments")


class ExperimentListView(ListAPIView):
    """List all experiments for a user."""
    queryset = Experiment.objects.all()
    serializer_class = ExperimentSerializer
    permission_classes = (IsAuthenticated,)


class ProjectExperimentListView(ListCreateAPIView):
    """
    get:
        List experiments under a project.

    post:
        Create an experiment under a project.
    """
    queryset = queries.experiments
    serializer_class = BookmarkedExperimentSerializer
    metrics_serializer_class = ExperimentLastMetricSerializer
    declarations_serializer_class = ExperimentDeclarationsSerializer
    create_serializer_class = ExperimentCreateSerializer
    permission_classes = (IsAuthenticated,)
    filter_backends = (QueryFilter, OrderingFilter,)
    query_manager = 'experiment'
    ordering = ('-updated_at',)
    ordering_fields = ('created_at', 'updated_at', 'started_at', 'finished_at')
    ordering_proxy_fields = {'metric': 'last_metric'}

    def get_serializer_class(self):
        if self.create_serializer_class and self.request.method.lower() == 'post':
            return self.create_serializer_class

        metrics_only = to_bool(self.request.query_params.get('metrics', None),
                               handle_none=True,
                               exception=ValidationError)
        if metrics_only:
            return self.metrics_serializer_class

        declarations_only = to_bool(self.request.query_params.get('declarations', None),
                                    handle_none=True,
                                    exception=ValidationError)
        if declarations_only:
            return self.declarations_serializer_class

        return self.serializer_class

    def get_group(self, project, group_id):
        group = get_object_or_404(ExperimentGroup, project=project, id=group_id)
        auditor.record(event_type=EXPERIMENT_GROUP_EXPERIMENTS_VIEWED,
                       instance=group,
                       actor_id=self.request.user.id,
                       actor_name=self.request.user.username)

        return group

    def filter_queryset(self, queryset):
        independent = to_bool(self.request.query_params.get('independent', None),
                              handle_none=True,
                              exception=ValidationError)
        group_id = self.request.query_params.get('group', None)
        if independent and group_id:
            raise ValidationError('You cannot filter for independent experiments and '
                                  'group experiments at the same time.')
        project = get_permissible_project(view=self)
        queryset = queryset.filter(project=project)
        if independent:
            queryset = queryset.filter(experiment_group__isnull=True)
        if group_id:
            group = self.get_group(project=project, group_id=group_id)
            if group.is_study:
                queryset = queryset.filter(experiment_group=group)
            elif group.is_selection:
                queryset = group.selection_experiments.all()
            else:
                raise ValidationError('Invalid group.')
        auditor.record(event_type=PROJECT_EXPERIMENTS_VIEWED,
                       instance=project,
                       actor_id=self.request.user.id,
                       actor_name=self.request.user.username)
        return super().filter_queryset(queryset=queryset)

    def perform_create(self, serializer):
        ttl = self.request.data.get(RedisTTL.TTL_KEY)
        if ttl:
            try:
                ttl = RedisTTL.validate_ttl(ttl)
            except ValueError:
                raise ValidationError('ttl must be an integer.')
        project = get_permissible_project(view=self)
        group = self.request.data.get('experiment_group')
        if group:
            try:
                group = ExperimentGroup.objects.get(id=group, project=project)
            except ExperimentGroup.DoesNotExist:
                raise ValidationError('Received an invalid group.')
            if group.is_selection:
                self.request.data.pop('experiment_group')

        instance = serializer.save(user=self.request.user, project=project)
        if group and group.is_selection:  # Add the experiment to the group selection
            group.selection_experiments.add(instance)
        auditor.record(event_type=EXPERIMENT_CREATED, instance=instance)
        if ttl:
            RedisTTL.set_for_experiment(experiment_id=instance.id, value=ttl)


class ExperimentDetailView(AuditorMixinView, RetrieveUpdateDestroyAPIView):
    """
    get:
        Get an experiment details.
    patch:
        Update an experiment details.
    delete:
        Delete an experiment.
    """
    queryset = queries.experiments_details
    serializer_class = ExperimentDetailSerializer
    permission_classes = (IsAuthenticated,)
    lookup_field = 'id'
    instance = None
    get_event = EXPERIMENT_VIEWED
    update_event = EXPERIMENT_UPDATED
    delete_event = EXPERIMENT_DELETED_TRIGGERED

    def filter_queryset(self, queryset):
        return queryset.filter(project=get_permissible_project(view=self))


class ExperimentCloneView(CreateAPIView):
    queryset = Experiment.objects.all()
    serializer_class = ExperimentSerializer
    permission_classes = (IsAuthenticated,)
    lookup_field = 'id'
    event_type = None

    def filter_queryset(self, queryset):
        return queryset.filter(project=get_permissible_project(view=self))

    def clone(self, obj, config, declarations, update_code_reference, description):
        pass

    def post(self, request, *args, **kwargs):
        ttl = self.request.data.get(RedisTTL.TTL_KEY)
        if ttl:
            try:
                ttl = RedisTTL.validate_ttl(ttl)
            except ValueError:
                raise ValidationError('ttl must be an integer.')

        obj = self.get_object()
        auditor.record(event_type=self.event_type,
                       instance=obj,
                       actor_id=self.request.user.id,
                       actor_name=self.request.user.username)

        description = None
        config = None
        declarations = None
        update_code_reference = False
        if 'config' in request.data:
            spec = validate_experiment_spec_config(
                [obj.specification.parsed_data, request.data['config']], raise_for_rest=True)
            config = spec.parsed_data
            declarations = spec.declarations
        if 'update_code' in request.data:
            update_code_reference = to_bool(request.data['update_code'],
                                            handle_none=True,
                                            exception=ValidationError)
        if 'description' in request.data:
            description = request.data['description']
        new_obj = self.clone(obj=obj,
                             config=config,
                             declarations=declarations,
                             update_code_reference=update_code_reference,
                             description=description)
        if ttl:
            RedisTTL.set_for_experiment(experiment_id=new_obj.id, value=ttl)
        serializer = self.get_serializer(new_obj)
        return Response(status=status.HTTP_201_CREATED, data=serializer.data)


class ExperimentRestartView(ExperimentCloneView):
    """Restart an experiment."""
    queryset = Experiment.objects.all()
    serializer_class = ExperimentSerializer
    permission_classes = (IsAuthenticated,)
    lookup_field = 'id'
    event_type = EXPERIMENT_RESTARTED_TRIGGERED

    def clone(self, obj, config, declarations, update_code_reference, description):
        return obj.restart(user=self.request.user,
                           config=config,
                           declarations=declarations,
                           update_code_reference=update_code_reference,
                           description=description)


class ExperimentResumeView(ExperimentCloneView):
    """Resume an experiment."""
    queryset = Experiment.objects.all()
    serializer_class = ExperimentSerializer
    permission_classes = (IsAuthenticated,)
    lookup_field = 'id'
    event_type = EXPERIMENT_RESUMED_TRIGGERED

    def clone(self, obj, config, declarations, update_code_reference, description):
        return obj.resume(user=self.request.user,
                          config=config,
                          declarations=declarations,
                          update_code_reference=update_code_reference,
                          description=description)


class ExperimentCopyView(ExperimentCloneView):
    """Copy an experiment."""
    queryset = Experiment.objects.all()
    serializer_class = ExperimentSerializer
    permission_classes = (IsAuthenticated,)
    lookup_field = 'id'
    event_type = EXPERIMENT_COPIED_TRIGGERED

    def clone(self, obj, config, declarations, update_code_reference, description):
        return obj.copy(user=self.request.user,
                        config=config,
                        declarations=declarations,
                        update_code_reference=update_code_reference,
                        description=description)


class ExperimentCodeReferenceView(CreateAPIView, RetrieveAPIView):
    """
    post:
        Create an experiment metric.
    """
    queryset = Experiment.objects.all()
    serializer_class = CodeReferenceSerializer
    permission_classes = (IsAuthenticated,)
    lookup_field = 'id'

    def perform_create(self, serializer):
        experiment = self.get_object()
        instance = serializer.save()
        experiment.code_reference = instance
        experiment.save(update_fields=['code_reference'])

    def retrieve(self, request, *args, **kwargs):
        instance = self.get_object()
        serializer = self.get_serializer(instance.code_reference)
        return Response(serializer.data)


class ExperimentViewMixin(object):
    """A mixin to filter by experiment."""
    project = None
    experiment = None

    def get_experiment(self):
        # Get project and check access
        self.project = get_permissible_project(view=self)
        experiment_id = self.kwargs['experiment_id']
        self.experiment = get_object_or_404(queries.experiments_auditing,
                                            project=self.project,
                                            id=experiment_id)
        return self.experiment

    def filter_queryset(self, queryset):
        queryset = super().filter_queryset(queryset)
        return queryset.filter(experiment=self.get_experiment())


class ExperimentOutputsTreeView(ExperimentViewMixin, RetrieveAPIView):
    """
    get:
        Returns a the outputs directory tree.
    """
    permission_classes = (IsAuthenticated,)

    def get(self, request, *args, **kwargs):
        experiment = self.get_experiment()
        store_manager = get_outputs_store(persistence_outputs=experiment.persistence_outputs)
        experiment_outputs_path = get_experiment_outputs_path(
            persistence_outputs=experiment.persistence_outputs,
            experiment_name=experiment.unique_name,
            original_name=experiment.original_unique_name,
            cloning_strategy=experiment.cloning_strategy)
        if request.query_params.get('path'):
            experiment_outputs_path = os.path.join(experiment_outputs_path,
                                                   request.query_params.get('path'))
        try:
            data = store_manager.ls(experiment_outputs_path)
        except VolumeNotFoundError:
            raise ValidationError('Store manager could not load the volume requested,'
                                  ' to get the outputs data.')
        except Exception:
            raise ValidationError('Experiment outputs path does not exists or bad configuration.')
        return Response(data=data, status=200)


class ExperimentOutputsFilesView(ExperimentViewMixin, RetrieveAPIView):
    """
    get:
        Returns a the outputs files content.
    """
    permission_classes = (IsAuthenticated,)

    def get(self, request, *args, **kwargs):
        filepath = request.query_params.get('path')
        if not filepath:
            raise ValidationError('Files view expect a path to the file.')

        experiment = self.get_experiment()
        experiment_outputs_path = get_experiment_outputs_path(
            persistence_outputs=experiment.persistence_outputs,
            experiment_name=experiment.unique_name,
            original_name=experiment.original_unique_name,
            cloning_strategy=experiment.cloning_strategy)

        download_filepath = archive_outputs_file(persistence_outputs=experiment.persistence_outputs,
                                                 outputs_path=experiment_outputs_path,
                                                 namepath=experiment.unique_name,
                                                 filepath=filepath)

        filename = os.path.basename(download_filepath)
        chunk_size = 8192
        try:
            wrapped_file = FileWrapper(open(download_filepath, 'rb'), chunk_size)
            response = StreamingHttpResponse(
                wrapped_file, content_type=mimetypes.guess_type(download_filepath)[0])
            response['Content-Length'] = os.path.getsize(download_filepath)
            response['Content-Disposition'] = "attachment; filename={}".format(filename)
            return response
        except FileNotFoundError:
            _logger.warning('Log file not found: log_path=%s', download_filepath)
            return Response(status=status.HTTP_404_NOT_FOUND,
                            data='Log file not found: log_path={}'.format(download_filepath))


class ExperimentStatusListView(ExperimentViewMixin, ListCreateAPIView):
    """
    get:
        List all statuses of an experiment.
    post:
        Create an experiment status.
    """
    queryset = ExperimentStatus.objects.order_by('created_at').all()
    serializer_class = ExperimentStatusSerializer
    permission_classes = (IsAuthenticated,)

    def perform_create(self, serializer):
        serializer.save(experiment=self.get_experiment())

    def get(self, request, *args, **kwargs):
        response = super().get(request, *args, **kwargs)
        auditor.record(event_type=EXPERIMENT_STATUSES_VIEWED,
                       instance=self.experiment,
                       actor_id=request.user.id,
                       actor_name=request.user.username)
        return response


class ExperimentMetricListView(ExperimentViewMixin, ListCreateAPIView):
    """
    get:
        List all metrics of an experiment.
    post:
        Create an experiment metric.
    """
    queryset = ExperimentMetric.objects.all()
    serializer_class = ExperimentMetricSerializer
    authentication_classes = api_settings.DEFAULT_AUTHENTICATION_CLASSES + [
        InternalAuthentication,
    ]
    permission_classes = (IsAuthenticatedOrInternal,)
    pagination_class = LargeLimitOffsetPagination
    throttle_scope = 'high'

    def perform_create(self, serializer):
        serializer.save(experiment=self.get_experiment())

    def get_serializer(self, *args, **kwargs):
        """ if an array is passed, set serializer to many """
        if isinstance(kwargs.get('data', {}), list):
            kwargs['many'] = True
        return super().get_serializer(*args, **kwargs)

    def create(self, request, *args, **kwargs):
        if isinstance(request.data, list):
            celery_app.send_task(
                SchedulerCeleryTasks.EXPERIMENTS_SET_METRICS,
                kwargs={
                    'experiment_id': self.get_experiment().id,
                    'data': request.data
                })
            return Response(status=status.HTTP_201_CREATED)

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)
        headers = self.get_success_headers(serializer.data)
        return Response(serializer.data, status=status.HTTP_201_CREATED, headers=headers)

    def get(self, request, *args, **kwargs):
        response = super().get(request, *args, **kwargs)
        auditor.record(event_type=EXPERIMENT_METRICS_VIEWED,
                       instance=self.experiment,
                       actor_id=request.user.id,
                       actor_name=request.user.username)
        return response


class ExperimentStatusDetailView(ExperimentViewMixin, RetrieveAPIView):
    """Get experiment status details."""
    queryset = ExperimentStatus.objects.all()
    serializer_class = ExperimentStatusSerializer
    permission_classes = (IsAuthenticated,)
    lookup_field = 'uuid'


class ExperimentJobListView(ExperimentViewMixin, ListCreateAPIView):
    """
    get:
        List all jobs of an experiment.
    post:
        Create an experiment job.
    """
    queryset = ExperimentJob.objects.order_by('-updated_at').all()
    serializer_class = ExperimentJobSerializer
    create_serializer_class = ExperimentJobDetailSerializer
    permission_classes = (IsAuthenticated,)

    def perform_create(self, serializer):
        serializer.save(experiment=self.get_experiment())

    def get(self, request, *args, **kwargs):
        response = super().get(request, *args, **kwargs)
        auditor.record(event_type=EXPERIMENT_JOBS_VIEWED,
                       instance=self.experiment,
                       actor_id=request.user.id,
                       actor_name=request.user.username)
        return response


class ExperimentJobDetailView(AuditorMixinView, ExperimentViewMixin, RetrieveUpdateDestroyAPIView):
    """
    get:
        Get experiment job details.
    patch:
        Update an experiment job details.
    delete:
        Delete an experiment job.
    """
    queryset = ExperimentJob.objects.all()
    serializer_class = ExperimentJobDetailSerializer
    permission_classes = (IsAuthenticated,)
    lookup_field = 'id'
    get_event = EXPERIMENT_JOB_VIEWED


class ExperimentLogsView(ExperimentViewMixin, RetrieveAPIView, PostAPIView):
    """
    get:
        Get experiment logs.
    post:
        Post experiment logs.
    """
    permission_classes = (IsAuthenticated,)

    def get(self, request, *args, **kwargs):
        experiment = self.get_experiment()
        auditor.record(event_type=EXPERIMENT_LOGS_VIEWED,
                       instance=self.experiment,
                       actor_id=request.user.id,
                       actor_name=request.user.username)
        log_path = get_experiment_logs_path(experiment.unique_name)

        filename = os.path.basename(log_path)
        chunk_size = 8192
        try:
            wrapped_file = FileWrapper(open(log_path, 'rb'), chunk_size)
            response = StreamingHttpResponse(wrapped_file,
                                             content_type=mimetypes.guess_type(log_path)[0])
            response['Content-Length'] = os.path.getsize(log_path)
            response['Content-Disposition'] = "attachment; filename={}".format(filename)
            return response
        except FileNotFoundError:
            _logger.warning('Log file not found: log_path=%s', log_path)
            return Response(status=status.HTTP_404_NOT_FOUND,
                            data='Log file not found: log_path={}'.format(log_path))

    def post(self, request, *args, **kwargs):
        experiment = self.get_experiment()
        log_lines = request.data
        if not log_lines or not isinstance(log_lines, (str, list)):
            raise ValidationError('Logs handler expects `data` to be a string or list of strings.')
        if isinstance(log_lines, list):
            log_lines = '\n'.join(log_lines)
        celery_app.send_task(
            LogsCeleryTasks.LOGS_HANDLE_EXPERIMENT_JOB,
            kwargs={
                'experiment_name': experiment.unique_name,
                'experiment_uuid': experiment.uuid.hex,
                'log_lines': log_lines
            })
        return Response(status=status.HTTP_200_OK)


class ExperimentHeartBeatView(ExperimentViewMixin, PostAPIView):
    """
    post:
        Post a heart beat ping.
    """
    permission_classes = (IsAuthenticatedOrInternal,)
    authentication_classes = api_settings.DEFAULT_AUTHENTICATION_CLASSES + [
        InternalAuthentication,
    ]

    def post(self, request, *args, **kwargs):
        experiment = self.get_experiment()
        RedisHeartBeat.experiment_ping(experiment_id=experiment.id)
        return Response(status=status.HTTP_200_OK)


class ExperimentJobViewMixin(object):
    """A mixin to filter by experiment job."""
    project = None
    experiment = None
    job = None

    def get_experiment(self):
        # Get project and check access
        self.project = get_permissible_project(view=self)
        experiment_id = self.kwargs['experiment_id']
        self.experiment = get_object_or_404(Experiment, project=self.project, id=experiment_id)
        return self.experiment

    def get_job(self):
        job_id = self.kwargs['id']
        self.job = get_object_or_404(ExperimentJob,
                                     id=job_id,
                                     experiment=self.get_experiment())
        return self.job

    def filter_queryset(self, queryset):
        queryset = super().filter_queryset(queryset)
        return queryset.filter(job=self.get_job())


class ExperimentJobStatusListView(ExperimentJobViewMixin, ListCreateAPIView):
    """
    get:
        List all statuses of experiment job.
    post:
        Create an experiment job status.
    """
    queryset = ExperimentJobStatus.objects.order_by('created_at').all()
    serializer_class = ExperimentJobStatusSerializer
    permission_classes = (IsAuthenticated,)

    def perform_create(self, serializer):
        serializer.save(job=self.get_job())

    def get(self, request, *args, **kwargs):
        response = super().get(request, *args, **kwargs)
        auditor.record(event_type=EXPERIMENT_JOB_STATUSES_VIEWED,
                       instance=self.job,
                       actor_id=request.user.id,
                       actor_name=request.user.username)
        return response


class ExperimentJobStatusDetailView(ExperimentJobViewMixin, RetrieveUpdateAPIView):
    """
    get:
        Get experiment job status details.
    patch:
        Update an experiment job status details.
    """
    queryset = ExperimentJobStatus.objects.all()
    serializer_class = ExperimentJobStatusSerializer
    permission_classes = (IsAuthenticated,)
    lookup_field = 'uuid'


class ExperimentStopView(CreateAPIView):
    """Stop an experiment."""
    queryset = Experiment.objects.all()
    serializer_class = ExperimentSerializer
    permission_classes = (IsAuthenticated,)
    lookup_field = 'id'

    def filter_queryset(self, queryset):
        return queryset.filter(project=get_permissible_project(view=self))

    def post(self, request, *args, **kwargs):
        obj = self.get_object()
        auditor.record(event_type=EXPERIMENT_STOPPED_TRIGGERED,
                       instance=obj,
                       actor_id=request.user.id,
                       actor_name=request.user.username)
        group = obj.experiment_group
        celery_app.send_task(
            SchedulerCeleryTasks.EXPERIMENTS_STOP,
            kwargs={
                'project_name': obj.project.unique_name,
                'project_uuid': obj.project.uuid.hex,
                'experiment_name': obj.unique_name,
                'experiment_uuid': obj.uuid.hex,
                'experiment_group_name': group.unique_name if group else None,
                'experiment_group_uuid': group.uuid.hex if group else None,
                'specification': obj.config,
                'update_status': True
            })
        return Response(status=status.HTTP_200_OK)


class ExperimentStopManyView(PostAPIView):
    """Stop a group of experiments."""
    queryset = Project.objects.all()
    permission_classes = (IsAuthenticated, IsProjectOwnerOrPublicReadOnly)
    lookup_field = 'name'

    def filter_queryset(self, queryset):
        username = self.kwargs['username']
        return queryset.filter(user__username=username)

    def post(self, request, *args, **kwargs):
        project = self.get_object()
        experiments = queries.experiments_auditing.filter(project=project,
                                                          id__in=request.data.get('ids', []))
        for experiment in experiments:
            auditor.record(event_type=EXPERIMENT_STOPPED_TRIGGERED,
                           instance=experiment,
                           actor_id=request.user.id,
                           actor_name=request.user.username)
            group = experiment.experiment_group
            celery_app.send_task(
                SchedulerCeleryTasks.EXPERIMENTS_STOP,
                kwargs={
                    'project_name': project.unique_name,
                    'project_uuid': project.uuid.hex,
                    'experiment_name': experiment.unique_name,
                    'experiment_uuid': experiment.uuid.hex,
                    'experiment_group_name': group.unique_name if group else None,
                    'experiment_group_uuid': group.uuid.hex if group else None,
                    'specification': experiment.config,
                    'update_status': True
                })
        return Response(status=status.HTTP_200_OK)


class ExperimentDeleteManyView(PostAPIView):
    """Delete a group of experiments."""
    queryset = Project.objects.all()
    permission_classes = (IsAuthenticated, IsProjectOwnerOrPublicReadOnly)
    lookup_field = 'name'

    def filter_queryset(self, queryset):
        username = self.kwargs['username']
        return queryset.filter(user__username=username)

    def delete(self, request, *args, **kwargs):
        project = self.get_object()
        experiments = queries.experiments_auditing.filter(project=project,
                                                          id__in=request.data.get('ids', []))
        for experiment in experiments:
            auditor.record(event_type=EXPERIMENT_DELETED_TRIGGERED,
                           instance=experiment,
                           actor_id=self.request.user.id,
                           actor_name=self.request.user.username)
            experiment.delete()
        return Response(status=status.HTTP_200_OK)


class ExperimentDownloadOutputsView(ProtectedView):
    """Download outputs of an experiment."""
    permission_classes = (IsAuthenticated,)
    HANDLE_UNAUTHENTICATED = False

    def get_object(self):
        project = get_permissible_project(view=self)
        experiment = get_object_or_404(Experiment, project=project, id=self.kwargs['id'])
        auditor.record(event_type=EXPERIMENT_OUTPUTS_DOWNLOADED,
                       instance=experiment,
                       actor_id=self.request.user.id,
                       actor_name=self.request.user.username)
        return experiment

    def get(self, request, *args, **kwargs):
        experiment = self.get_object()
        archived_path, archive_name = archive_experiment_outputs(
            persistence_outputs=experiment.persistence_outputs,
            experiment_name=experiment.unique_name)
        return self.redirect(path='{}/{}'.format(archived_path, archive_name))


class ExperimentScopeTokenView(PostAPIView):
    """Validate scope token and return user's token."""
    queryset = Experiment.objects.all()
    authentication_classes = [EphemeralAuthentication, ]
    permission_classes = (IsEphemeral,)
    throttle_scope = 'ephemeral'
    lookup_field = 'id'

    def post(self, request, *args, **kwargs):
        user = request.user

        if user.scope is None:
            return Response(status=status.HTTP_403_FORBIDDEN)

        experiment = self.get_object()

        if experiment.last_status not in [ExperimentLifeCycle.SCHEDULED,
                                          ExperimentLifeCycle.STARTING,
                                          ExperimentLifeCycle.RUNNING]:
            return Response(status=status.HTTP_403_FORBIDDEN)

        scope = RedisEphemeralTokens.get_scope(user=experiment.user.id,
                                               model='experiment',
                                               object_id=experiment.id)
        if sorted(user.scope) != sorted(scope):
            return Response(status=status.HTTP_403_FORBIDDEN)

        token, _ = Token.objects.get_or_create(user=experiment.user)
        return Response({'token': token.key}, status=status.HTTP_200_OK)


class ExperimentChartViewListView(ExperimentViewMixin, ListCreateAPIView):
    """
    get:
        List all chart views of an experiment.
    post:
        Create an experiment chart view.
    """
    queryset = ExperimentChartView.objects.all()
    serializer_class = ExperimentChartViewSerializer
    permission_classes = (IsAuthenticated,)
    pagination_class = LargeLimitOffsetPagination

    def perform_create(self, serializer):
        experiment = self.get_experiment()
        instance = serializer.save(experiment=experiment)
        auditor.record(event_type=CHART_VIEW_CREATED,
                       instance=instance,
                       actor_id=self.request.user.id,
                       actor_name=self.request.user.username,
                       experiment=experiment)


class ExperimentChartViewDetailView(ExperimentViewMixin, RetrieveUpdateDestroyAPIView):
    """
    get:
        Get experiment chart view details.
    patch:
        Update an experiment chart view details.
    delete:
        Delete an experiment chart view.
    """
    queryset = ExperimentChartView.objects.all()
    serializer_class = ExperimentChartViewSerializer
    permission_classes = (IsAuthenticated,)
    lookup_field = 'id'
    delete_event = CHART_VIEW_DELETED

    def get_object(self):
        instance = super().get_object()
        method = self.request.method.lower()
        if method == 'delete' and self.delete_event:
            auditor.record(event_type=self.delete_event,
                           instance=instance,
                           actor_id=self.request.user.id,
                           actor_name=self.request.user.username,
                           group=instance)
        return instance
