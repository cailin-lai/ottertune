import logging

from collections import OrderedDict
from pytz import timezone

from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm
from django.core.cache import cache
from django.core.exceptions import ObjectDoesNotExist
from django.http import Http404, HttpResponse, QueryDict
from django.shortcuts import redirect, render, get_object_or_404
from django.template.context_processors import csrf
from django.template.defaultfilters import register
from django.urls import reverse, reverse_lazy
from django.utils.datetime_safe import datetime
from django.utils.timezone import now
from django.views.decorators.csrf import csrf_exempt
from djcelery.models import TaskMeta

from .forms import NewResultForm, ProjectForm, SessionForm
from .models import (BackupData, DBMSCatalog, Hardware, KnobCatalog,
                     KnobData, MetricCatalog, MetricData, MetricManager,
                     PipelineResult,Project, Result, Session, Workload)
from tasks import (aggregate_target_results,
                   map_workload,
                   configuration_recommendation)
from .types import (DBMSType, HardwareType, KnobUnitType, MetricType,
                    PipelineTaskType, TaskType, VarType)
from .utils import DBMSUtil, JSONUtil, LabelUtil, MediaUtil, TaskUtil

log = logging.getLogger(__name__)


# For the html template to access dict object
@register.filter
def get_item(dictionary, key):
    return dictionary.get(key)


# def ajax_new(request):
#     new_id = request.GET['new_id']
#     data = {}
# #     ts = Statistics.objects.filter(data_result=new_id,
# #                                    type=StatsType.SAMPLES)
# #     metric_meta = {}
# #     for metric, metric_info in metric_meta.iteritems():
# #         if len(ts) > 0:
# #             offset = ts[0].time
# #             if len(ts) > 1:
# #                 offset -= ts[1].time - ts[0].time
# #             data[metric] = []
# #             for t in ts:
# #                 data[metric].append(
# #                     [t.time - offset,
# #                         getattr(t, metric) * metric_info.scale])
#     return HttpResponse(JSONUtil.dumps(data), content_type='application/json')


def signup_view(request):
    if request.user.is_authenticated():
        return redirect(reverse('home_projects'))
    if request.method == 'POST':
        post = request.POST
        form = UserCreationForm(post)
        if form.is_valid():
            form.save()
            new_post = QueryDict(mutable=True)
            new_post.update(post)
            new_post['password'] = post['password1']
            request.POST = new_post
            return login_view(request)
        else:
            log.warn(form.is_valid())
            log.warn(form.errors)
    else:
        form = UserCreationForm()
    token = {}
    token.update(csrf(request))
    token['form'] = form

    return render(request, 'signup.html', token)


def login_view(request):
    if request.user.is_authenticated():
        return redirect(reverse('home_projects'))
    if request.method == 'POST':
        post = request.POST
        form = AuthenticationForm(None, post)
        if form.is_valid():
            login(request, form.get_user())
            return redirect(reverse('home_projects'))
        else:
            log.info("Invalid request: {}".format(
                ', '.join(form.error_messages)))
    else:
        form = AuthenticationForm()
    token = {}
    token.update(csrf(request))
    token['form'] = form

    return render(request, 'login.html', token)


@login_required(login_url=reverse_lazy('login'))
def logout_view(request):
    logout(request)
    return redirect(reverse('login'))


@login_required(login_url=reverse_lazy('login'))
def redirect_home(request):
    return redirect(reverse('home_projects'))


@login_required(login_url=reverse_lazy('login'))
def home_projects_view(request):
    form_labels = Project.get_labels()
    form_labels.update(LabelUtil.style_labels({
        'button_create': 'create a new project',
        'button_delete': 'delete selected projects',
    }))
    form_labels['title'] = 'Your Projects'
    projects = Project.objects.filter(user=request.user)
    show_descriptions = False
    for proj in projects:
        if proj.description != None and proj.description != "":
            show_descriptions = True
            break
    context = {
        "projects": projects,
        "labels": form_labels,
        "show_descriptions": show_descriptions
    }
    context.update(csrf(request))
    return render(request, 'home_projects.html', context)


@login_required(login_url=reverse_lazy('login'))
def create_or_edit_project(request, project_id=''):
    if request.method == 'POST':
        if project_id == '':
            form = ProjectForm(request.POST)
            if not form.is_valid():
                return HttpResponse(str(form))
            project = form.save(commit=False)
            project.user = request.user
            ts = now()
            project.creation_time = ts
            project.last_update = ts
            project.save()
        else:
            project = Project.objects.get(pk=int(project_id))
            if project.user != request.user:
                return Http404()
            form = ProjectForm(request.POST, instance=project)
            if not form.is_valid():
                return HttpResponse(str(form))
            project.last_update = now()
            project.save()
        return redirect(reverse('project_sessions', kwargs={'project_id': project.pk}))
    else:
        if project_id == '':
            project = None
            form = ProjectForm()
        else:
            project = Project.objects.get(pk=int(project_id))
            form = ProjectForm(instance=project)
        context = {
            'project': project,
            'form': form,
        }
        return render(request, 'edit_project.html', context)


@login_required(login_url=reverse_lazy('login'))
def delete_project(request):
    for pk in request.POST.getlist('projects', []):
        project = Project.objects.get(pk=pk)
        if project.user == request.user:
            project.delete()
    return redirect(reverse('home_projects'))


@login_required(login_url=reverse_lazy('login'))
def project_sessions_view(request, project_id):
    sessions = Session.objects.filter(project=project_id)
    project = Project.objects.get(pk=project_id)
    form_labels = Session.get_labels()
    form_labels.update(LabelUtil.style_labels({
        'button_delete': 'delete selected session',
        'button_create': 'create a new session',
    }))
    form_labels['title'] = "Your Sessions"
    context = {
        "sessions": sessions,
        "project": project,
        "labels": form_labels,
    }
    context.update(csrf(request))
    return render(request, 'project_sessions.html', context)


@login_required(login_url=reverse_lazy('login'))
def session_view(request, project_id, session_id):
    project = get_object_or_404(Project, pk=project_id)
    session = get_object_or_404(Session, pk=session_id)

    # All results from this session
    results = Result.objects.filter(session=session)

    # Group the session's results by DBMS & workload
    dbmss = {}
    workloads = {}
    for res in results:
        dbmss[res.dbms.key] = res.dbms
        workload_name = res.workload.name
        if workload_name not in workloads:
            workloads[workload_name] = set()
        workloads[workload_name].add(res.workload)

    # Sort so names will be ordered in the sidebar
    workloads = OrderedDict([(k, sorted(list(v))) for \
                             k, v in sorted(workloads.iteritems())])
    dbmss = OrderedDict(sorted(dbmss.items()))

    if len(workloads) > 0:
        # Set the default workload to whichever is first
        default_workload, default_confs = workloads.iteritems().next()
        default_confs = ','.join([str(c.pk) for c in default_confs])
    else:
        # Set the default to display nothing if there are no results yet
        default_workload = 'show_none'
        default_confs = 'none'

    default_metrics = MetricCatalog.objects.get_default_metrics(session.target_objective)
    metric_meta = MetricCatalog.objects.get_metric_meta(session.dbms, True)

    form_labels = Session.get_labels()
    form_labels['title'] = "Session Info"
    context = {
        'project': project,
        'dbmss': dbmss,
        'workloads': workloads,
        'results_per_page': [10, 50, 100],
        'default_dbms': session.dbms.key,
        'default_results_per_page': 10,
        'default_equidistant': "on",
        'default_workload': default_workload,
        'defaultspe': default_confs,
        'metrics': metric_meta.keys(),
        'metric_meta': metric_meta,
        'default_metrics': default_metrics,
        'filters': [],
        'session': session,
        'results': results,
        'labels': form_labels,
    }
    context.update(csrf(request))
    return render(request, 'session.html', context)


@login_required(login_url=reverse_lazy('login'))
def create_or_edit_session(request, project_id, session_id=''):
    project = get_object_or_404(Project, pk=project_id)
    if request.method == 'POST':
        if session_id == '':
            # Create a new session from the form contents
            form = SessionForm(request.POST)
            if not form.is_valid():
                return HttpResponse(str(form))
            session = form.save(commit=False)
            session.user = request.user
            session.project = project
            ts = now()
            session.creation_time = ts
            session.last_update = ts
            session.upload_code = MediaUtil.upload_code_generator()
            session.save()
        else:
            # Update an existing session with the form contents
            session = Session.objects.get(pk=int(session_id))
            form = SessionForm(request.POST, instance=session)
            if not form.is_valid():
                return HttpResponse(str(form))
            if form.cleaned_data['gen_upload_code'] is True:
                session.upload_code = MediaUtil.upload_code_generator()
            session.last_update = now()
            session.save()
        return redirect(reverse('session', kwargs={'project_id': project_id,
                                                   'session_id': session.pk}))
    else:
        if project.user != request.user:
            return Http404()
        if session_id != '':
            # Return a pre-filled form for editing an existing session
            session = Session.objects.get(pk=session_id)
            form = SessionForm(instance=session)
        else:
            # Return a new form with defaults for creating a new session
            session = None
            form = SessionForm(
                initial={
                    'dbms': DBMSCatalog.objects.get(
                        type=DBMSType.POSTGRES, version='9.6'),
                    'hardware': Hardware.objects.get(
                        type=HardwareType.EC2_M3XLARGE),
                    'target_objective': 'throughput_txn_per_sec',
                })
        context = {
            'project': project,
            'session': session,
            'form': form,
        }
        return render(request, 'edit_session.html', context)


@login_required(login_url=reverse_lazy('login'))
def delete_session(request, project_id):
    for session_id in request.POST.getlist('sessions', []):
        session = Session.objects.get(pk=session_id)
        if session.user == request.user:
            session.delete()
    return redirect(reverse(
        'project_sessions',
        kwargs={'project_id': project_id}))


@login_required(login_url=reverse_lazy('login'))
def result_view(request, project_id, session_id, result_id):
    target = get_object_or_404(Result, pk=result_id)
    session = target.session
#     if session.user != request.user:
#         raise Http404()
#     data_package = {}
#     results = Result.objects.filter(session=session,
#                                     dbms=session.dbms,
#                                     workload=target.workload)

    # Find other results with the same knob config as the target
#     same_knob_configs = filter(lambda res: res.pk != target.pk and
#                                result_same(res, target), results)
# 
#     # Find other results with knob configs similar to the target.
#     # We consider two knob configs similar they have the same
#     # settings for the top 3 knobs and are not the same.
#     ranked_knobs = JSONUtil.loads(PipelineResult.get_latest(
#         session.dbms, session.hardware, PipelineTaskType.RANKED_KNOBS).value)[:3]
#     similar_knob_configs = filter(
#         lambda res: res.pk not in([target.pk] + [r.pk for r in same_knob_configs]) and
#         result_similar(res, target, ranked_knobs), results)
# 
#     metric_meta = MetricCatalog.objects.get_metric_meta(session.dbms, True)
#     for metric, metric_info in metric_meta.iteritems():
#         data_package[metric] = {
#             'data': {},
#             'units': metric_info.unit,
#             'lessisbetter': metric_info.improvement,
#             'metric': metric_info.pprint,
#             'print': metric_info.pprint,
#         }

#         same_id = [str(target.pk)]
#         for x in same_id:
#             key = metric + ',data,' + x
#             tmp = cache.get(key)
#             if tmp is not None:
#                 data_package[metric]['data'][int(x)] = []
#                 data_package[metric]['data'][int(x)].extend(tmp)
#                 continue

            # We no longer collect timeseries data (but this may change)
#             ts = Statistics.objects.filter(data_result=x, type=StatsType.SAMPLES)
#             if len(ts) > 0:
#                 offset = ts[0].time
#                 if len(ts) > 1:
#                     offset -= ts[1].time - ts[0].time
#                 data_package[metric]['data'][int(x)] = []
#                 for t in ts:
#                     data_package[metric]['data'][int(x)].append(
#                         [t.time - offset, getattr(t, metric) * metric_info.scale])
#                 cache.set(key, data_package[metric]['data'][int(x)], 60 * 5)

    default_metrics = MetricCatalog.objects.get_default_metrics(session.target_objective)
    metric_meta = MetricCatalog.objects.get_metric_meta(session.dbms, True)
    metric_data = JSONUtil.loads(target.metric_data.data)

    default_metrics = {mname: metric_data[mname] * metric_meta[mname].scale
                       for mname in default_metrics}

    status = None
    if target.task_ids is not None:
        task_ids = target.task_ids.split(',')
        tasks = []
        for tid in task_ids:
            task = TaskMeta.objects.filter(task_id=tid).first()
            if task is not None:
                tasks.append(task)
        status, _ = TaskUtil.get_task_status(tasks)
        if status is None:
            status = 'UNAVAILABLE'

    next_conf_available = True if status == 'SUCCESS' else False
    form_labels = Result.get_labels()
    form_labels.update(LabelUtil.style_labels({
        'status': 'status',
        'next_conf_available': 'next configuration'
    }))
    form_labels['title'] = 'Result Info'
    context = {
        'result': target,
        'metric_meta': metric_meta,
#         'default_metrics': default_metrics,
#         'data': JSONUtil.dumps({}),#data_package),
#         'same_runs': [],#same_knob_configs,
        'status': status,
        'next_conf_available': next_conf_available,
#         'similar_runs': [],#similar_knob_configs,
        'labels': form_labels,
        'project_id': project_id,
        'session_id': session_id
    }
    return render(request, 'result.html', context)


@csrf_exempt
def new_result(request):
    if request.method == 'POST':
        form = NewResultForm(request.POST, request.FILES)

        if not form.is_valid():
            log.warning("Form is not valid:\n" + str(form))
            return HttpResponse("Form is not valid\n" + str(form))
        upload_code = form.cleaned_data['upload_code']
        try:
            session = Session.objects.get(upload_code=upload_code)
        except Session.DoesNotExist:
            log.warning("Wrong upload code: " + upload_code)
            return HttpResponse("wrong upload_code!")

        return handle_result_files(session, request.FILES)
    log.warning("Request type was not POST")
    return HttpResponse("Request type was not POST")


def handle_result_files(session, files):
    from celery import chain

    # Combine into contiguous files
    files = {k: ''.join(v.chunks()) for k, v in files.iteritems()}

    # Load the contents of the controller's summary file
    summary = JSONUtil.loads(files['summary'])
    dbms_type = DBMSType.type(summary['database_type'])
#     dbms_version = DBMSUtil.parse_version_string(
#        dbms_type, summary['database_version'])
    dbms_version = '9.6'  ## FIXME (dva)
    workload_name = summary['workload_name']
    observation_time = summary['observation_time']
    start_time = datetime.fromtimestamp(
        int(summary['start_time']) / 1000,
        timezone("UTC"))
    end_time = datetime.fromtimestamp(
        int(summary['end_time']) / 1000,
        timezone("UTC"))
    try:
        # Check that we support this DBMS and version
        dbms = DBMSCatalog.objects.get(
            type=dbms_type, version=dbms_version)
    except ObjectDoesNotExist:
        return HttpResponse('{} v{} is not yet supported.'.format(
            dbms_type, dbms_version))

    if dbms != session.dbms:
        return HttpResponse('The DBMS must match the type and version '
                            'specified when creating the session. '
                            '(expected=' + session.dbms.full_name + ') '
                            '(actual=' + dbms.full_name + ')')

    # Load, process, and store the knobs in the DBMS's configuration
    knob_dict, knob_diffs = DBMSUtil.parse_dbms_config(
        dbms.pk, JSONUtil.loads(files['knobs']))
    tunable_knob_dict = DBMSUtil.convert_dbms_params(
        dbms.pk, knob_dict)
    knob_data = KnobData.objects.create_knob_data(
        session, JSONUtil.dumps(knob_dict, pprint=True, sort=True),
        JSONUtil.dumps(tunable_knob_dict, pprint=True, sort=True), dbms)

    # Load, process, and store the runtime metrics exposed by the DBMS
    initial_metric_dict, initial_metric_diffs = DBMSUtil.parse_dbms_metrics(
            dbms.pk, JSONUtil.loads(files['metrics_before']))
    final_metric_dict, final_metric_diffs = DBMSUtil.parse_dbms_metrics(
            dbms.pk, JSONUtil.loads(files['metrics_after']))
    metric_dict = DBMSUtil.calculate_change_in_metrics(
        dbms.pk, initial_metric_dict, final_metric_dict)
    initial_metric_diffs.extend(final_metric_diffs)
    numeric_metric_dict = DBMSUtil.convert_dbms_metrics(
        dbms.pk, metric_dict, observation_time)
    metric_data = MetricData.objects.create_metric_data(
        session, JSONUtil.dumps(metric_dict, pprint=True, sort=True),
        JSONUtil.dumps(numeric_metric_dict, pprint=True, sort=True), dbms)

    # Create a new workload if this one does not already exist
    workload = Workload.objects.create_workload(
        dbms, session.hardware, workload_name)

    # Save this result
    result = Result.objects.create_result(
        session, dbms, workload, knob_data, metric_data,
        start_time, end_time, observation_time)
    result.save()

    # Save all original data
    backup_data = BackupData.objects.create(
        result=result, raw_knobs=files['knobs'],
        raw_initial_metrics=files['metrics_before'],
        raw_final_metrics=files['metrics_after'],
        raw_summary=files['summary'],
        knob_log=knob_diffs,
        metric_log=initial_metric_diffs)
    backup_data.save()

    nondefault_settings = DBMSUtil.get_nondefault_settings(
        dbms.pk, knob_dict)
    session.project.last_update = now()
    session.last_update = now()
    if session.nondefault_settings is None:
        session.nondefault_settings = JSONUtil.dumps(nondefault_settings)
    session.project.save()
    session.save()

    if session.tuning_session is False:
        return HttpResponse("Result stored successfully!")

    response = chain(aggregate_target_results.s(result.pk),
                     map_workload.s(),
                     configuration_recommendation.s()).apply_async()
    taskmeta_ids = [response.parent.parent.id, response.parent.id, response.id]
    result.task_ids = ','.join(taskmeta_ids)
    result.save()
    return HttpResponse("Result stored successfully! Running tuner... (status={})".format(
        response.status))


@login_required(login_url=reverse_lazy('login'))
def dbms_knobs_reference(request, dbms_name, version, knob_name):
    knob = get_object_or_404(KnobCatalog, dbms__type=DBMSType.type(dbms_name),
                              dbms__version=version, name=knob_name)
    labels = KnobCatalog.get_labels()
    list_items = OrderedDict()
    if knob.category is not None:
        list_items[labels['category']] = knob.category
    list_items[labels['scope']] = knob.scope
    list_items[labels['tunable']] = knob.tunable
    list_items[labels['vartype']] = VarType.name(knob.vartype)
    if knob.unit != KnobUnitType.OTHER:
        list_items[labels['unit']] = knob.unit
    list_items[labels['default']] = knob.default
    if knob.minval is not None:
        list_items[labels['minval']] = knob.minval
    if knob.maxval is not None:
        list_items[labels['maxval']] = knob.maxval
    if knob.enumvals is not None:
        list_items[labels['enumvals']] = knob.enumvals
    if knob.summary is not None:
        description = knob.summary
        if knob.description is not None:
            description += knob.description
        list_items[labels['summary']] = description
    
    context = {
        'title': knob.name,
        'dbms': knob.dbms,
        'is_used': knob.tunable,
        'used_label': 'TUNABLE',
        'list_items': list_items,
    }
    return render(request, 'dbms_reference.html', context)


@login_required(login_url=reverse_lazy('login'))
def dbms_metrics_reference(request, dbms_name, version, metric_name):
    metric = get_object_or_404(
        MetricCatalog, dbms__type=DBMSType.type(dbms_name),
        dbms__version=version, name=metric_name)
    labels = MetricCatalog.get_labels()
    list_items = OrderedDict()
    list_items[labels['scope']] = metric.scope
    list_items[labels['vartype']] = VarType.name(metric.vartype)
    list_items[labels['summary']] = metric.summary
    context = {
        'title': metric.name,
        'dbms': metric.dbms,
        'is_used': metric.metric_type == MetricType.COUNTER,
        'used_label': MetricType.name(metric.metric_type),
        'list_items': list_items,
    }
    return render(request, 'dbms_reference.html', context=context)


@login_required(login_url=reverse_lazy('login'))
def knob_data_view(request, project_id, session_id, data_id):
    knob_data = get_object_or_404(KnobData, pk=data_id)
    labels = KnobData.get_labels()
    labels.update(LabelUtil.style_labels({
        'featured_data': 'tunable dbms parameters',
        'all_data': 'all dbms parameters',
    }))
    labels['title'] = 'DBMS Configuration'
    context = {
        'labels': labels,
        'data_type': 'knobs'
    }
    return dbms_data_view(request, context, knob_data)


@login_required(login_url=reverse_lazy('login'))
def metric_data_view(request, project_id, session_id, data_id):
    metric_data = get_object_or_404(MetricData, pk=data_id)
    labels = MetricData.get_labels()
    labels.update(LabelUtil.style_labels({
        'featured_data': 'numeric dbms metrics',
        'all_data': 'all dbms metrics',
    }))
    labels['title'] = 'DBMS Metrics'
    context = {
        'labels': labels,
        'data_type': 'metrics'
    }
    return dbms_data_view(request, context, metric_data)


def dbms_data_view(request, context, dbms_data):
    if context['data_type'] == 'knobs':
        model_class = KnobData
        filter_fn = DBMSUtil.filter_tunable_params
        obj_data = dbms_data.knobs
        addl_args = []
    else:
        model_class = MetricData
        filter_fn = DBMSUtil.filter_numeric_metrics
        obj_data = dbms_data.metrics
        addl_args = [True]

    dbms_id = dbms_data.dbms.pk
    all_data_dict = JSONUtil.loads(obj_data)
    args = [dbms_id, all_data_dict] + addl_args
    featured_dict = filter_fn(*args)

    if 'compare' in request.GET and request.GET['compare'] != 'none':
        comp_id = request.GET['compare']
        compare_obj = model_class.objects.get(pk=comp_id)
        comp_data = compare_obj.knobs if \
            context['data_type'] == 'knobs' else compare_obj.metrics
        comp_dict = JSONUtil.loads(comp_data)
        args = [dbms_id, comp_dict] + addl_args
        comp_featured_dict = filter_fn(*args)

        all_data = [(k, v, comp_dict[k]) for k, v in all_data_dict.iteritems()]
        featured_data = [(k, v, comp_featured_dict[k])
                         for k, v in featured_dict.iteritems()]
    else:
        comp_id = None
        all_data = list(all_data_dict.iteritems())
        featured_data = list(featured_dict.iteritems())
    peer_data = model_class.objects.filter(
        dbms=dbms_data.dbms, session=dbms_data.session)
    peer_data = filter(lambda peer: peer.pk != dbms_data.pk, peer_data)

    context['all_data'] = all_data
    context['featured_data'] = featured_data
    context['dbms_data'] = dbms_data
    context['compare'] = comp_id
    context['peer_data'] = peer_data
    return render(request, 'dbms_data.html', context)


@login_required(login_url=reverse_lazy('login'))
def workload_view(request, project_id, session_id, wkld_id):
    workload = get_object_or_404(Workload, pk=wkld_id)
    session = get_object_or_404(Session, pk=session_id)

    db_confs = KnobData.objects.filter(dbms=session.dbms,
                                       session=session)
    all_db_confs = []
    conf_map = {}
    for conf in db_confs:
        results = Result.objects.filter(session=session,
                                        knob_data=conf,
                                        workload=workload)
        if len(results) == 0:
            continue
        result = results.latest('observation_end_time')
        all_db_confs.append(conf.pk)
        conf_map[conf.name] = [conf, result]
    conf_map = OrderedDict(sorted(conf_map.iteritems()))
    all_db_confs = [c for c, _ in conf_map.values()][:5]

    metric_meta = MetricCatalog.objects.get_metric_meta(session.dbms, True)
    default_metrics = MetricCatalog.objects.get_default_metrics(session.target_objective)

    labels = Workload.get_labels()
    labels['title'] = 'Workload Information'
    context = {'workload': workload,
               'confs': conf_map,
               'metric_meta': metric_meta,
               'knob_data': all_db_confs,
               'default_metrics': default_metrics,
               'labels': labels,
               'proj_id': project_id,
               'session_id': session_id}
    return render(request, 'workload.html', context)


@login_required(login_url=reverse_lazy('login'))
def tuner_status_view(request, project_id, session_id, result_id):
    res = Result.objects.get(pk=result_id)

    task_ids = res.task_ids.split(',')
    tasks = []
    for tid in task_ids:
        task = TaskMeta.objects.filter(task_id=tid).first()
        if task is not None:
            tasks.append(task)

    overall_status, num_completed = TaskUtil.get_task_status(tasks)
    if overall_status in ['PENDING', 'RECEIVED', 'STARTED']:
        completion_time = 'N/A'
        total_runtime = 'N/A'
    else:
        completion_time = tasks[-1].date_done
        total_runtime = (completion_time - res.creation_time).total_seconds()
        total_runtime = '{0:.2f} seconds'.format(total_runtime)

    task_info = [(tname, task) for tname, task in \
                 zip(TaskType.TYPE_NAMES.values(), tasks)]

    context = {"id": result_id,
               "result": res,
               "overall_status": overall_status,
               "num_completed": "{} / {}".format(num_completed, 3),
               "completion_time": completion_time,
               "total_runtime": total_runtime,
               "tasks": task_info}

    return render(request, "task_status.html", context)

# Data Format
#    error
#    metrics as a list of selected metrics
#    results
#        data for each selected metric
#            meta data for the metric
#            Result list for the metric in a folded list
@login_required(login_url=reverse_lazy('login'))
def get_workload_data(request):
    data = request.GET

    workload = get_object_or_404(Workload, pk=data['id'])
    session = get_object_or_404(Session, pk=data['session_id'])
    if session.user != request.user:
        return render(request, '404.html')

    results = Result.objects.filter(workload=workload)
    result_data = {r.pk: JSONUtil.loads(r.metric_data.data) for r in results}
    results = sorted(results, cmp=lambda x, y: int(result_data[y.pk][MetricManager.THROUGHPUT] -
                                                   result_data[x.pk][MetricManager.THROUGHPUT]))

    default_metrics = MetricCatalog.objects.get_default_metrics(session.target_objective)
    metrics = request.GET.get('met', ','.join(default_metrics)).split(',')
    metrics = [m for m in metrics if m != 'none']
    if len(metrics) == 0:
        metrics = default_metrics
        
    data_package = {'results': [],
                    'error': 'None',
                    'metrics': metrics}
    metric_meta = MetricCatalog.objects.get_metric_meta(session.dbms, True)
    for met in data_package['metrics']:
        met_info = metric_meta[met]
        data_package['results'].append({'data': [[]], 'tick': [],
                                        'unit': met_info.unit,
                                        'lessisbetter': met_info.improvement,
                                        'metric': met_info.pprint})

        added = {}
        db_confs = data['db'].split(',')
        i = len(db_confs)
        for r in results:
            metric_data = JSONUtil.loads(r.metric_data.data)
            if r.knob_data.pk in added or str(r.knob_data.pk) not in db_confs:
                continue
            added[r.knob_data.pk] = True
            data_val = metric_data[met] * met_info.scale
            data_package['results'][-1]['data'][0].append([
                i,
                data_val,
                r.pk,
                data_val])
            data_package['results'][-1]['tick'].append(r.knob_data.name)
            i -= 1
        data_package['results'][-1]['data'].reverse()
        data_package['results'][-1]['tick'].reverse()

    return HttpResponse(JSONUtil.dumps(data_package), content_type='application/json')


def result_similar(a, b, compare_params):
    dbms_id = a.dbms.pk
    db_conf_a = DBMSUtil.filter_tunable_params(
        dbms_id, JSONUtil.loads(a.knob_data.knobs))
    db_conf_b = DBMSUtil.filter_tunable_params(
        dbms_id, JSONUtil.loads(b.knob_data.knobs))
    for param in compare_params:
        if db_conf_a[param] != db_conf_b[param]:
            return False
    return True


def result_same(a, b):
    dbms_id = a.dbms.pk
    db_conf_a = DBMSUtil.filter_tunable_params(
        dbms_id, JSONUtil.loads(a.knob_data.knobs))
    db_conf_b = DBMSUtil.filter_tunable_params(
        dbms_id, JSONUtil.loads(b.knob_data.knobs))
    for k, v in db_conf_a.iteritems():
        if k not in db_conf_b or v != db_conf_b[k]:
            return False
    return True


@login_required(login_url=reverse_lazy('login'))
def update_similar(request):
    raise Http404()


# Data Format:
#    error
#    results
#        all result data after the filters for the table
#    timelines
#        data for each benchmark & metric pair
#            meta data for the pair
#            data as a map<DBMS name, result list>
@login_required(login_url=reverse_lazy('login'))
def get_timeline_data(request):
    result_labels = Result.get_labels()
    columnnames = [
        result_labels['id'],
        result_labels['creation_time'],
        result_labels['knob_data'],
        result_labels['metric_data'],
        result_labels['workload'],
    ]
    data_package = {
        'error': 'None',
        'timelines': [], 
        'columnnames': columnnames,
    }

    session = get_object_or_404(Session, pk=request.GET['session'])
    if session.user != request.user:
        return HttpResponse(JSONUtil.dumps(data_package), content_type='application/json')

    default_metrics = MetricCatalog.objects.get_default_metrics(session.target_objective)

    metric_meta = MetricCatalog.objects.get_metric_meta(session.dbms, True)
    for met in default_metrics:
        met_info = metric_meta[met]
        columnnames.append(
            met_info.pprint + ' (' + 
            met_info.short_unit + ')') 

    results_per_page = int(request.GET['nres'])

    # Get all results related to the selected session, sort by time
    results = Result.objects.filter(session=session)
    results = sorted(results, cmp=lambda x, y: int(
        (x.observation_end_time - y.observation_end_time).total_seconds()))

    display_type = request.GET['wkld']
    if display_type == 'show_none':
        workloads = []
        metrics = default_metrics
        results = []
        pass
    else:
        metrics = request.GET.get(
            'met', ','.join(default_metrics)).split(',')
        metrics = [m for m in metrics if m != 'none']
        if len(metrics) == 0:
            metrics = default_metrics
        workloads = [display_type]
        workload_confs = filter(lambda x: x != '', request.GET[
                                 'spe'].strip().split(','))
        results = filter(lambda x: str(x.workload.pk)
                         in workload_confs, results)

    metric_datas = {r.pk: JSONUtil.loads(r.metric_data.data) for r in results}
    result_list = []
    for x in results:
        entry = [
            x.pk,
            x.observation_end_time.strftime("%Y-%m-%d %H:%M:%S"),
            x.knob_data.name,
            x.metric_data.name,
            x.workload.name]
        for met in metrics:
            entry.append(metric_datas[x.pk][met] * metric_meta[met].scale)
        entry.extend([
            '',
            x.knob_data.pk,
            x.metric_data.pk,
            x.workload.pk
        ])
        result_list.append(entry)
    data_package['results'] = result_list

    # For plotting charts
    for metric in metrics:
        met_info = metric_meta[metric]
        for wkld in workloads:
            w_r = filter(lambda x: x.workload.name == wkld, results)
            if len(w_r) == 0:
                continue

            data = {
                'workload': wkld,
                'units': met_info.unit,
                'lessisbetter': met_info.improvement,
                'data': {},
                'baseline': "None",
                'metric': metric,
                'print_metric': met_info.pprint,
            }

            for dbms in request.GET['dbms'].split(','):
                d_r = filter(lambda x: x.dbms.key == dbms, w_r)
                d_r = d_r[-results_per_page:]
                out = []
                for res in d_r:
                    metric_data = JSONUtil.loads(res.metric_data.data)
                    out.append([
                        res.observation_end_time.strftime("%m-%d-%y %H:%M"),
                        metric_data[metric] * met_info.scale,
                        "",
                        str(res.pk)
                    ])

                if len(out) > 0:
                    data['data'][dbms] = out

            data_package['timelines'].append(data)

    return HttpResponse(JSONUtil.dumps(data_package), content_type='application/json')
