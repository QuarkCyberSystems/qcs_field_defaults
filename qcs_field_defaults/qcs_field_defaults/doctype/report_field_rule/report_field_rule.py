# Copyright (c) 2026, QCS and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from qcs_field_defaults.report_field_guard import clear_rule_cache


class ReportFieldRule(Document):
	def autoname(self):
		# A controller autoname() wins over the DocType's `autoname` property, which
		# cannot branch on whether this rule targets a report or a doctype.
		target = self.report or self.reference_doctype
		if target:
			self.name = f"{target}-Report-Hidden"

	def validate(self):
		self.resolve_target()
		self.populate_field_metadata()

	def resolve_target(self):
		"""A rule targets one report, or every report on a DocType — not neither."""
		if self.report:
			ref = frappe.db.get_value("Report", self.report, "ref_doctype")
			if self.reference_doctype and self.reference_doctype != ref:
				frappe.throw(
					_("Report {0} reports on {1}, not {2}.").format(self.report, ref, self.reference_doctype)
				)
			# Derived so the field pickers still have a DocType to work from.
			self.reference_doctype = ref
		elif not self.reference_doctype:
			frappe.throw(_("Set either a Report or a DocType for this rule to apply to."))

	def on_update(self):
		clear_rule_cache()

	def on_trash(self):
		clear_rule_cache()

	def populate_field_metadata(self):
		"""Fill in label and fieldtype from the doctype, where the column is a real field.

		Query and Script Reports routinely emit *computed* columns that exist
		nowhere in the schema — VIN Stock Report's `valuation_rate` and
		`total_valuation` are built in Python, not stored on Serial No. Those are
		exactly the columns worth hiding, so a fieldname that is not a DocField is
		accepted and flagged as a report column rather than rejected.
		"""
		if not self.reference_doctype:
			return

		meta_cache = {}
		for row in self.fields:
			if not row.fieldname:
				frappe.throw(_("Row {0}: Fieldname is required").format(row.idx))

			row.fieldname = row.fieldname.strip()
			dt = row.source_doctype or self.reference_doctype
			row.source_doctype = dt

			if dt not in meta_cache:
				try:
					meta_cache[dt] = frappe.get_meta(dt)
				except Exception:
					frappe.throw(_("Row {0}: DocType '{1}' does not exist").format(row.idx, dt))

			df = meta_cache[dt].get_field(row.fieldname)
			if df:
				row.label = df.label
				row.fieldtype = df.fieldtype
			else:
				row.label = row.label or row.fieldname
				row.fieldtype = "Report Column"


@frappe.whitelist()
def preview_rule(reference_doctype, user=None, report=None):
	"""Show what a given user loses, and which reports this rule actually covers.

	The reports list matters: a rule only applies to reports whose Reference
	DocType matches, which is the easiest thing to get wrong. VIN Stock Report,
	for example, reports on Serial No — a rule built against Stock Entry never
	touches it.
	"""
	frappe.only_for("System Manager")

	from qcs_field_defaults.report_field_guard import get_hidden_fieldnames

	user = user or frappe.session.user
	hidden = sorted(get_hidden_fieldnames(reference_doctype, user, report))
	meta = frappe.get_meta(reference_doctype)

	fields = []
	for fieldname in hidden:
		df = meta.get_field(fieldname)
		fields.append(
			{
				"fieldname": fieldname,
				"label": df.label if df else fieldname,
				"source": _("DocType field") if df else _("Report column"),
			}
		)

	# A report-targeted rule covers exactly one report; a doctype rule covers all
	# reports on that doctype.
	if report:
		reports = frappe.get_all(
			"Report", filters={"name": report}, fields=["name", "report_type"]
		)
	else:
		reports = frappe.get_all(
			"Report",
			filters={"ref_doctype": reference_doctype, "disabled": 0},
			fields=["name", "report_type"],
			order_by="name",
		)

	return {"fields": fields, "reports": reports}


def _fallback_filters() -> dict:
	"""Filters to retry with when a report refuses to run bare.

	Report filters are declared in the report's *client* script, so the server
	cannot enumerate them — VIN Stock Report's `company` is `reqd: 1` and it
	throws "Please select a Company" before producing any columns. These are the
	filters that are mandatory often enough to be worth guessing; anything else
	has to be supplied by the caller.
	"""
	filters = {}
	if company := frappe.defaults.get_user_default("Company"):
		filters["company"] = company
	today = frappe.utils.today()
	filters["from_date"] = frappe.utils.add_months(today, -1)
	filters["to_date"] = today
	return filters


@frappe.whitelist()
def get_report_columns(report_name, filters=None):
	"""Return the columns a report actually emits, so they can be picked directly.

	Picking from the DocType only offers stored fields, which misses the two kinds
	of column most worth hiding: ones the report computes itself (VIN Stock
	Report's `total_valuation`) and ones a user bolted on with the report's
	"Add Column" menu, which are saved into `Report.json` and carry a `link_field`.

	Saved columns are read straight off the document. For Query and Script Reports
	the remaining columns only exist once the report has run, so it is executed —
	with the caller's filters if given, otherwise bare and then again with
	`_fallback_filters()`. If it still will not run, the saved columns are returned
	along with the error and the filters that were tried, so the caller can supply
	the missing ones.
	"""
	frappe.only_for("System Manager")

	filters = frappe.parse_json(filters) if filters else None

	report = frappe.get_doc("Report", report_name)
	seen, columns, error = set(), [], None

	def _add(col, source):
		if not isinstance(col, dict):
			from frappe.desk.query_report import get_column_as_dict

			col = get_column_as_dict(col)
		fieldname = col.get("fieldname")
		if not fieldname or fieldname in seen:
			return
		seen.add(fieldname)
		columns.append(
			{
				"fieldname": fieldname,
				"label": col.get("label") or fieldname,
				"fieldtype": col.get("fieldtype") or "Data",
				"source": source,
			}
		)

	# Columns saved on the report: Report Builder rows, plus anything added
	# through "Add Column" and saved.
	for row in report.get("columns") or []:
		_add({"fieldname": row.fieldname, "label": row.label, "fieldtype": row.fieldtype}, _("Saved column"))

	if report.json:
		try:
			for col in (frappe.parse_json(report.json) or {}).get("columns") or []:
				_add(col, _("Added column") if col.get("link_field") else _("Saved column"))
		except Exception:
			pass

	tried_filters = None
	if report.report_type in ("Query Report", "Script Report"):
		from frappe.desk import query_report

		attempts = [filters] if filters else [None, _fallback_filters()]
		for attempt in attempts:
			try:
				result = query_report.run(report_name, filters=attempt, ignore_prepared_report=True)
				for col in result.get("columns") or []:
					_add(col, _("Report output"))
				error, tried_filters = None, attempt
				break
			except Exception as e:
				# Keep the last failure; a later attempt may still succeed.
				error, tried_filters = str(e), attempt
			finally:
				# A failed report run leaves its own message on the response, which
				# would surface as a confusing popup next to our own error text.
				frappe.clear_messages()

	return {
		"columns": columns,
		"error": error,
		"ref_doctype": report.ref_doctype,
		"filters_used": tried_filters,
		"needs_filters": bool(error) and not columns,
	}


@frappe.whitelist()
def get_reports_for_doctype(reference_doctype):
	"""Reports whose Reference DocType is this one — i.e. what a rule here covers."""
	frappe.only_for("System Manager")

	return frappe.get_all(
		"Report",
		filters={"ref_doctype": reference_doctype, "disabled": 0},
		fields=["name", "report_type"],
		order_by="name",
	)
