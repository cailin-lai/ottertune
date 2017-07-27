import time

from celery.task import task, Task
from django.utils.timezone import now

from website.models import Task as TaskModel, ResultData


class UpdateTask(Task):

    def __call__(self, *args, **kwargs):
        self.rate_limit = '50/m'
        self.max_retries = 3
        self.default_retry_delay = 60
        
        # Update start time for this task
        task = TaskModel.objects.get(taskmeta_id=self.request.id)
        task.start_time = now()
        task.save()
        return super(UpdateTask, self).__call__(*args, **kwargs)
    
#     def after_return(self, status, retval, task_id, args, kwargs, einfo):
#         super(UpdateTask, self).after_return(status, retval, task_id, args, kwargs, einfo)
#         print "RETURNED!! (task_id={}, rl={}, mr={}, drt={})".format(task_id, self.rate_limit, self.max_retries, self.default_retry_delay)
#     
#     def on_failure(self, exc, task_id, args, kwargs, einfo):
#         super(UpdateTask, self).on_failure(exc, task_id, args, kwargs, einfo)
#         print "FAILURE!! {} (task_id={})".format(exc, task_id)
#     
#     def on_success(self, retval, task_id, args, kwargs):
#         super(UpdateTask, self).on_success(retval, task_id, args, kwargs)
#         print "SUCCESS!! result={} (task_id={})".format(retval, task_id)
#     
#     def on_retry(self, exc, task_id, args, kwargs, einfo):
#         super(UpdateTask, self).on_retry(exc, task_id, args, kwargs, einfo)
#         print "RETRY!! {} (task_id={})".format(exc, task_id)

@task(base=UpdateTask, name='preprocess')
def preprocess(a, b):
    print "PREPROCESSING ({}, {})".format(a, b)
    time.sleep(2)
    return a + b

@task(base=UpdateTask, name='run_wm')
def run_wm(q, r):
    print "RUNNING WM: ({}, {})".format(q, r)
    time.sleep(3)
    return q + r

@task(base=UpdateTask, name='run_gpr')
def run_gpr(x, y):
    print "RUNNING GP ({}, {})".format(x, y)
    time.sleep(4)
    return x + y
    
    
    



