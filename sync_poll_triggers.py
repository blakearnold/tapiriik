from tapiriik.database import db, close_connections
from tapiriik.requests_lib import patch_requests_source_address
from tapiriik.settings import RABBITMQ_BROKER_URL, MONGO_HOST
from tapiriik import settings
from datetime import datetime

if isinstance(settings.HTTP_SOURCE_ADDR, list):
    settings.HTTP_SOURCE_ADDR = settings.HTTP_SOURCE_ADDR[0]
    patch_requests_source_address((settings.HTTP_SOURCE_ADDR, 0))

from tapiriik.services import Service
from celery import Celery
from celery.signals import worker_shutdown
from datetime import datetime

class _celeryConfig:
	CELERY_ROUTES = {
		"sync_poll_triggers.trigger_poll": {"queue": "tapiriik-poll"}
	}
	CELERYD_CONCURRENCY = 1 # Otherwise the GC rate limiting breaks since file locking is per-process.
	CELERYD_PREFETCH_MULTIPLIER = 1 # The message queue could use some exercise.

celery_app = Celery('sync_poll_triggers', broker=RABBITMQ_BROKER_URL)
celery_app.config_from_object(_celeryConfig())

@worker_shutdown.connect
def celery_shutdown():
	close_connections()

@celery_app.task()
def trigger_poll(service_id, index):
    svc = Service.FromID(service_id)
    affected_connection_external_ids = svc.PollPartialSyncTrigger(index)
    print("Triggering %d connections via %s-%d" % (len(affected_connection_external_ids), service_id, index))
    db.connections.update({"Service": service_id, "ExternalID": {"$in": affected_connection_external_ids}}, {"$set":{"TriggerPartialSync": True, "TriggerPartialSyncTimestamp": datetime.utcnow()}}, multi=True)
    db.poll_stats.insert({"Service": service_id, "Index": index, "Timestamp": datetime.utcnow(), "TriggerCount": len(affected_connection_external_ids)})

def schedule_trigger_poll():
	schedule_data = list(db.trigger_poll_scheduling.find())
	print("Scheduler run at %s" % datetime.now())
	for svc in Service.List():
		if svc.PartialSyncTriggerRequiresPolling:
			print("Checking %s's %d poll indexes" % (svc.ID, svc.PartialSyncTriggerPollMultiple))
			for idx in range(svc.PartialSyncTriggerPollMultiple):
				svc_schedule = [x for x in schedule_data if x["Service"] == svc.ID and x["Index"] == idx]
				if not svc_schedule:
					svc_schedule = {"Service": svc.ID, "Index": idx, "LastScheduled": datetime.min}
				else:
					svc_schedule = svc_schedule[0]

				if datetime.utcnow() - svc_schedule["LastScheduled"] > svc.PartialSyncTriggerPollInterval:
					print("Scheduling %s-%d" % (svc.ID, idx))
					svc_schedule["LastScheduled"] = datetime.utcnow()
					trigger_poll.apply_async(args=[svc.ID, idx], expires=svc.PartialSyncTriggerPollInterval.total_seconds(), time_limit=svc.PartialSyncTriggerPollInterval.total_seconds())
					db.trigger_poll_scheduling.update({"Service": svc.ID, "Index": idx}, svc_schedule, upsert=True)

if __name__ == "__main__":
	schedule_trigger_poll()
	close_connections()