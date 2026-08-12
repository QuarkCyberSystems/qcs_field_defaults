# Copyright (c) 2026, QCS and contributors
# For license information, please see license.txt

"""Strip restricted columns out of Query/Script Report output, server side.

Frappe already enforces field-level permissions for everything that goes through
`frappe.model.db_query` — List View, Report View, `frappe.client.get_list` and the
list export all drop fields the user has no permlevel access to. Query Reports and
Script Reports do not: `frappe.desk.query_report` checks only the doctype-level
`report` permission and its `get_filtered_data` filters *rows*, never columns. The
columns come straight out of the report's own SQL or Python, so a restricted field
can be read — and exported — through any Query Report.

This module closes that hole. `Report Field Rule` records name the fields to hide
— either for one named report, or for every report on a reference DocType — and
the columns (plus their data) are removed from the result before it leaves the
server. Nothing is hidden in the browser, so there is
nothing to re-enable from the console or read out of the network payload.

Why a monkeypatch rather than `override_whitelisted_methods`: the export path
(`export_query` -> `_export_query`, and the background `run_export_query_job`)
calls the *module-level* `run`, not the whitelisted entry point. Overriding the
whitelisted method would leave exports wide open. Rebinding the module attribute
covers the report view, the CSV/Excel export and the background export job in one
place, because all three resolve `run` from the module namespace at call time.
"""

import json

import frappe

CACHE_KEY = "qcs_report_field_rules"


def _load_rules() -> dict:
	"""Return rules bucketed by what they target.

	{"by_doctype": {ref_doctype: [rule, ...]}, "by_report": {report: [rule, ...]}}

	A rule with `report` set applies to that report alone; one without applies to
	every report whose Reference DocType matches.
	"""
	rules = frappe.get_all(
		"Report Field Rule",
		filters={"disabled": 0},
		fields=["name", "reference_doctype", "report"],
	)
	if not rules:
		return {}

	names = [r.name for r in rules]

	field_rows = frappe.get_all(
		"Report Field Rule Field",
		filters={"parenttype": "Report Field Rule", "parent": ["in", names]},
		fields=["parent", "fieldname"],
	)
	role_rows = frappe.get_all(
		"Report Field Rule Role",
		filters={"parenttype": "Report Field Rule", "parent": ["in", names]},
		fields=["parent", "role"],
	)

	by_rule = {n: {"fields": set(), "roles": set()} for n in names}
	for row in field_rows:
		if row.fieldname:
			by_rule[row.parent]["fields"].add(row.fieldname)
	for row in role_rows:
		if row.role:
			by_rule[row.parent]["roles"].add(row.role)

	out = {"by_doctype": {}, "by_report": {}}
	for rule in rules:
		entry = by_rule[rule.name]
		if not entry["fields"]:
			# A rule with no fields hides nothing; skip it so it cannot be
			# mistaken for "hide everything".
			continue
		payload = {"fields": sorted(entry["fields"]), "roles": sorted(entry["roles"])}
		if rule.report:
			out["by_report"].setdefault(rule.report, []).append(payload)
		elif rule.reference_doctype:
			out["by_doctype"].setdefault(rule.reference_doctype, []).append(payload)
	return out


def get_rules() -> dict:
	cached = frappe.cache().get_value(CACHE_KEY)
	if cached is None:
		cached = _load_rules()
		frappe.cache().set_value(CACHE_KEY, cached)
	return cached


def clear_rule_cache():
	frappe.cache().delete_value(CACHE_KEY)


def get_hidden_fieldnames(ref_doctype: str, user: str | None = None, report: str | None = None) -> set:
	"""Fieldnames this user may not see.

	Combines the rule for this specific report, if any, with the rules covering
	every report on `ref_doctype`.
	"""
	if not ref_doctype and not report:
		return set()

	user = user or frappe.session.user
	if user == "Administrator":
		return set()

	rules = get_rules()
	applicable = list(rules.get("by_doctype", {}).get(ref_doctype) or [])
	if report:
		applicable += rules.get("by_report", {}).get(report) or []

	if not applicable:
		return set()

	user_roles = set(frappe.get_roles(user))
	hidden = set()
	for rule in applicable:
		if rule["roles"] and user_roles.intersection(rule["roles"]):
			continue
		hidden.update(rule["fields"])
	return hidden


def _column_as_dict(column) -> dict:
	from frappe.desk.query_report import get_column_as_dict

	try:
		return get_column_as_dict(column)
	except Exception:
		# A malformed column must never take the whole report down; an
		# unparseable column has no fieldname to match a rule against.
		return {}


def _strip_rows(rows, keep_indexes: list, removed: set):
	if not rows:
		return rows

	stripped = []
	for row in rows:
		if isinstance(row, dict):
			# `result` rows are frappe._dict; rebuild with the same type so
			# downstream attribute access keeps working.
			cleaned = row.__class__((k, v) for k, v in row.items() if k not in removed)
			stripped.append(cleaned)
		elif isinstance(row, (list, tuple)):
			cleaned = [row[i] for i in keep_indexes if i < len(row)]
			stripped.append(row.__class__(cleaned) if isinstance(row, tuple) else cleaned)
		else:
			stripped.append(row)
	return stripped


def _mentions_hidden(value, tokens: set) -> bool:
	if not value:
		return False
	try:
		blob = value if isinstance(value, str) else json.dumps(value, default=str)
	except (TypeError, ValueError):
		return True  # cannot prove it is clean, so drop it
	return any(token in blob for token in tokens if token)


def strip_report_result(result, ref_doctype: str, user: str | None = None, report: str | None = None):
	"""Remove restricted columns, and their values, from a query report result."""
	if not isinstance(result, dict):
		return result

	hidden = get_hidden_fieldnames(ref_doctype, user, report)
	if not hidden:
		return result

	columns = result.get("columns") or []
	keep_indexes, keep_columns, removed = [], [], set()
	# Charts and summary tiles usually refer to a column by its *label*, not its
	# fieldname, so track both and match either.
	tokens = set()
	for idx, column in enumerate(columns):
		col_dict = _column_as_dict(column)
		fieldname = col_dict.get("fieldname")
		if fieldname and fieldname in hidden:
			removed.add(fieldname)
			tokens.add(fieldname)
			label = col_dict.get("label")
			if label:
				tokens.add(label)
				tokens.add(frappe.scrub(label))
			continue
		keep_indexes.append(idx)
		keep_columns.append(column)

	if not removed:
		return result

	result["columns"] = keep_columns
	result["result"] = _strip_rows(result.get("result"), keep_indexes, removed)

	# A chart or summary tile built on a hidden column hands the value straight
	# back — e.g. a "Total Valuation" tile survives removal of the
	# `total_valuation` column unless the label is matched too.
	if _mentions_hidden(result.get("chart"), tokens):
		result["chart"] = None
	if result.get("report_summary"):
		result["report_summary"] = [
			tile for tile in result["report_summary"] if not _summary_tile_is_hidden(tile, tokens)
		]

	return result


def _summary_tile_is_hidden(tile, tokens: set) -> bool:
	if not isinstance(tile, dict):
		return False
	if tile.get("fieldname") and tile["fieldname"] in tokens:
		return True
	label = tile.get("label")
	if label and (label in tokens or frappe.scrub(label) in tokens):
		return True
	return _mentions_hidden(tile, tokens)


def install():
	"""Idempotently wrap the query report entry points with the column guard."""
	from frappe.desk import query_report

	_install_run_guard(query_report)
	_install_custom_field_guard(query_report)


def _install_run_guard(query_report):
	original = query_report.run
	if getattr(original, "__qcs_report_guard__", False):
		return

	def guarded_run(report_name, *args, **kwargs):
		result = original(report_name, *args, **kwargs)
		try:
			ref_doctype = frappe.get_cached_value("Report", report_name, "ref_doctype")
			return strip_report_result(result, ref_doctype, kwargs.get("user"), report=report_name)
		except Exception:
			# Never leak on failure: if the guard cannot run, the report does not
			# either.
			frappe.log_error("Report Field Rule guard failed")
			raise

	guarded_run.__qcs_report_guard__ = True
	guarded_run.__name__ = original.__name__
	guarded_run.__doc__ = original.__doc__

	# The whitelist registry holds function objects, so the wrapper has to be
	# registered too or the desk call is rejected as not-whitelisted.
	query_report.run = frappe.whitelist()(guarded_run)


def _install_custom_field_guard(query_report):
	"""Close the "Add Column" side door.

	`get_data_for_custom_field` is whitelisted and checks only the doctype-level
	`read` permission, then returns the values of any field the caller names. It
	is what the report's Add Column menu calls, so without this a user can pull a
	hidden column's values straight out of the endpoint even though the report
	itself no longer shows it.
	"""
	original = query_report.get_data_for_custom_field
	if getattr(original, "__qcs_report_guard__", False):
		return

	def guarded_get_data_for_custom_field(doctype, field, names=None):
		if field in get_hidden_fieldnames(doctype):
			frappe.throw(
				frappe._("Not permitted to read {0} on {1}").format(field, doctype),
				frappe.PermissionError,
			)
		return original(doctype, field, names)

	guarded_get_data_for_custom_field.__qcs_report_guard__ = True
	guarded_get_data_for_custom_field.__name__ = original.__name__
	guarded_get_data_for_custom_field.__doc__ = original.__doc__

	query_report.get_data_for_custom_field = frappe.whitelist()(guarded_get_data_for_custom_field)
