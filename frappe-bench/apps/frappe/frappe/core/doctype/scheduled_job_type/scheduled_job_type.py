# Copyright (c) 2021, Frappe Technologies and contributors
# License: MIT. See LICENSE

import hashlib
import json
from datetime import datetime

import click
from croniter import CroniterBadCronError, croniter

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import get_datetime, now_datetime
from frappe.utils.background_jobs import enqueue, is_job_enqueued


class ScheduledJobType(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		create_log: DF.Check
		cron_format: DF.Data | None
		frequency: DF.Literal[
			"All",
			"Hourly",
			"Hourly Long",
			"Hourly Maintenance",
			"Daily",
			"Daily Long",
			"Daily Maintenance",
			"Weekly",
			"Weekly Long",
			"Monthly",
			"Monthly Long",
			"Cron",
			"Yearly",
			"Annual",
		]
		last_execution: DF.Datetime | None
		method: DF.Data
		scheduler_event: DF.Link | None
		server_script: DF.Link | None
		stopped: DF.Check

	# end: auto-generated types
	def autoname(self):
		self.name = ".".join(self.method.split(".")[-2:])

	def validate(self):
		if self.frequency != "All":
			# force logging for all events other than continuous ones (ALL)
			self.create_log = 1

		if self.frequency == "Cron":
			if not self.cron_format:
				frappe.throw(_("Cron format is required for job types with Cron frequency."))
			try:
				croniter(self.cron_format)
			except CroniterBadCronError:
				frappe.throw(
					_("{0} is not a valid Cron expression.").format(f"<code>{self.cron_format}</code>"),
					title=_("Bad Cron Expression"),
				)

	def enqueue(self, force=False) -> bool:
		# enqueue event if last execution is done
		if self.is_event_due() or force:
			if not self.is_job_in_queue():
				enqueue(
					"frappe.core.doctype.scheduled_job_type.scheduled_job_type.run_scheduled_job",
					queue=self.get_queue_name(),
					job_type=self.method,
					job_id=self.rq_job_id,
				)
				return True
			else:
				frappe.logger("scheduler").error(
					f"Skipped queueing {self.method} because it was found in queue for {frappe.local.site}"
				)

		return False

	def is_event_due(self, current_time=None):
		"""Return true if event is due based on time lapsed since last execution"""
		# if the next scheduled event is before NOW, then its due!
		return self.get_next_execution() <= (current_time or now_datetime())

	def is_job_in_queue(self) -> bool:
		return is_job_enqueued(self.rq_job_id)

	@property
	def rq_job_id(self):
		"""Unique ID created to deduplicate jobs with single RQ call."""
		return f"scheduled_job::{self.method}"

	@property
	def next_execution(self):
		return self.get_next_execution()

	def get_next_execution(self):
		# Maintenance jobs run at random time, the time is specific to the site though.
		# This is done to avoid scheduling all maintenance task on all sites at the same time in
		# multitenant deployments.
		hourly_site_offset = int(hashlib.sha1(frappe.local.site.encode()).hexdigest(), 16) % 60
		daily_site_offset = (hourly_site_offset + 30) % 60

		CRON_MAP = {
			"Yearly": "0 0 1 1 *",
			"Annual": "0 0 1 1 *",
			"Monthly": "0 0 1 * *",
			"Monthly Long": "0 0 1 * *",
			"Weekly": "0 0 * * 0",
			"Weekly Long": "0 0 * * 0",
			"Daily": "0 0 * * *",
			"Daily Long": "0 0 * * *",
			"Daily Maintenance": f"{daily_site_offset} 0 * * *",
			"Hourly": "0 * * * *",
			"Hourly Long": "0 * * * *",
			"Hourly Maintenance": f"{hourly_site_offset} * * * *",
			"All": f"*/{(frappe.get_conf().scheduler_interval or 240) // 60} * * * *",
		}

		if not self.cron_format:
			self.cron_format = CRON_MAP.get(self.frequency)

		# If this is a cold start then last_execution will not be set.
		# Creation is set as fallback because if very old fallback is set job might trigger
		# immediately, even when it's meant to be daily.
		# A dynamic fallback like current time might miss the scheduler interval and job will never start.
		last_execution = get_datetime(self.last_execution or self.creation)

		return croniter(self.cron_format, last_execution).get_next(datetime)

	def execute(self):
		self.scheduler_log = None
		try:
			self.log_status("Start")
			if self.server_script:
				script_name = frappe.db.get_value("Server Script", self.server_script)
				if script_name:
					frappe.get_doc("Server Script", script_name).execute_scheduled_method()
			else:
				frappe.get_attr(self.method)()
			frappe.db.commit()
			self.log_status("Complete")
		except Exception:
			frappe.db.rollback()
			self.log_status("Failed")

	def log_status(self, status):
		# log file
		frappe.logger("scheduler").info(f"Scheduled Job {status}: {self.method} for {frappe.local.site}")
		self.update_scheduler_log(status)

	def update_scheduler_log(self, status):
		if not self.create_log:
			# self.get_next_execution will work properly iff self.last_execution is properly set
			self.db_set("last_execution", now_datetime(), update_modified=False)
			frappe.db.commit()
			return
		if not self.scheduler_log:
			self.scheduler_log = frappe.get_doc(
				dict(doctype="Scheduled Job Log", scheduled_job_type=self.name)
			).insert(ignore_permissions=True)
		self.scheduler_log.db_set("status", status)
		if frappe.debug_log:
			self.scheduler_log.db_set("debug_log", "\n".join(frappe.debug_log))
		if status == "Failed":
			self.scheduler_log.db_set("details", frappe.get_traceback(with_context=True))
		if status == "Start":
			self.db_set("last_execution", now_datetime(), update_modified=False)
		frappe.db.commit()

	def get_queue_name(self):
		return "long" if ("Long" in self.frequency or "Maintenance" in self.frequency) else "default"

	def on_trash(self):
		frappe.db.delete("Scheduled Job Log", {"scheduled_job_type": self.name})


@frappe.whitelist()
def execute_event(doc: str):
	frappe.only_for("System Manager")
	doc = json.loads(doc)
	frappe.get_doc("Scheduled Job Type", doc.get("name")).enqueue(force=True)
	return doc


def run_scheduled_job(job_type: str):
	"""This is a wrapper function that runs a hooks.scheduler_events method"""
	if frappe.conf.maintenance_mode:
		raise frappe.InReadOnlyMode("Scheduled jobs can't run in maintenance mode.")
	try:
		frappe.get_doc("Scheduled Job Type", dict(method=job_type)).execute()
	except Exception:
		print(frappe.get_traceback())


def sync_jobs(hooks: dict | None = None):
	frappe.reload_doc("core", "doctype", "scheduled_job_type")
	scheduler_events = hooks or frappe.get_hooks("scheduler_events")
	all_events = insert_events(scheduler_events)
	clear_events(all_events)


def insert_events(scheduler_events: dict) -> list:
	cron_jobs, event_jobs = [], []
	for event_type in scheduler_events:
		events = scheduler_events.get(event_type)
		if isinstance(events, dict):
			cron_jobs += insert_cron_jobs(events)
		else:
			# hourly, daily etc
			event_jobs += insert_event_jobs(events, event_type)
	return cron_jobs + event_jobs


def insert_cron_jobs(events: dict) -> list:
	cron_jobs = []
	for cron_format in events:
		for event in events.get(cron_format):
			cron_jobs.append(event)
			insert_single_event("Cron", event, cron_format)
	return cron_jobs


def insert_event_jobs(events: list, event_type: str) -> list:
	event_jobs = []
	for event in events:
		event_jobs.append(event)
		frequency = event_type.replace("_", " ").title()
		insert_single_event(frequency, event)
	return event_jobs


def insert_single_event(frequency: str, event: str, cron_format: str | None = ""):
	try:
		frappe.get_attr(event)
	except Exception as e:
		click.secho(f"{event} is not a valid method: {e}", fg="yellow")
		return

	doc: ScheduledJobType

	if job_name := frappe.db.exists("Scheduled Job Type", {"method": event}):
		doc = frappe.get_doc("Scheduled Job Type", job_name)

		# Update only frequency and cron_format fields if they are different
		# Maintain existing values of other fields
		if doc.frequency != frequency or doc.cron_format != cron_format:
			doc.cron_format = cron_format
			doc.frequency = frequency
			doc.save()
	else:
		doc = frappe.get_doc(
			{
				"doctype": "Scheduled Job Type",
				"method": event,
				"cron_format": cron_format,
				"frequency": frequency,
			}
		)

		savepoint = "scheduled_job_type_creation"
		try:
			frappe.db.savepoint(savepoint)
			doc.insert()
		except frappe.DuplicateEntryError:
			frappe.db.rollback(save_point=savepoint)
			doc.delete()
			doc.insert()


def clear_events(all_events: list):
	for event in frappe.get_all(
		"Scheduled Job Type", fields=["name", "method", "server_script", "scheduler_event"]
	):
		is_server_script = event.server_script
		is_defined_in_hooks = event.method in all_events

		if event.scheduler_event:
			continue

		if not (is_defined_in_hooks or is_server_script):
			frappe.delete_doc("Scheduled Job Type", event.name)
