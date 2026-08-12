# Copyright (c) 2026, QCS and contributors
# See license.txt

import frappe
from frappe.tests.utils import FrappeTestCase

from qcs_field_defaults.report_field_guard import (
	clear_rule_cache,
	get_hidden_fieldnames,
	strip_report_result,
)

ROLE = "_Test Report Guard Role"
PLAIN_USER = "_test_report_guard_plain@example.com"
ALLOWED_USER = "_test_report_guard_allowed@example.com"
# Self-referential on purpose: this DocType grants System Manager the `report`
# permission in its own definition, so the end-to-end test cannot be broken by
# another app's permission changes.
RULE_DOCTYPE = "Report Field Rule"
HIDDEN_FIELD = "disabled"


def _make_user(email, roles):
	if not frappe.db.exists("User", email):
		user = frappe.get_doc(
			{"doctype": "User", "email": email, "first_name": "Report Guard Test"}
		).insert(ignore_permissions=True)
	else:
		user = frappe.get_doc("User", email)
	user.add_roles(*roles)
	return user


def _sample_result():
	return {
		"columns": [
			{"fieldname": "name", "label": "ID", "fieldtype": "Data"},
			{"fieldname": HIDDEN_FIELD, "label": "Description", "fieldtype": "Text"},
			{"fieldname": "status", "label": "Status", "fieldtype": "Data"},
		],
		"result": [
			frappe._dict({"name": "ROW-1", HIDDEN_FIELD: "secret", "status": "Open"}),
			frappe._dict({"name": "ROW-2", HIDDEN_FIELD: "also secret", "status": "Closed"}),
		],
	}


class TestReportFieldRule(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		if not frappe.db.exists("Role", ROLE):
			frappe.get_doc({"doctype": "Role", "role_name": ROLE}).insert(ignore_permissions=True)

		_make_user(PLAIN_USER, ["System Manager"])
		_make_user(ALLOWED_USER, ["System Manager", ROLE])

		frappe.db.delete("Report Field Rule", {"reference_doctype": RULE_DOCTYPE})
		rule = frappe.get_doc(
			{
				"doctype": "Report Field Rule",
				"reference_doctype": RULE_DOCTYPE,
				"allowed_roles": [{"role": ROLE}],
				"fields": [{"source_doctype": RULE_DOCTYPE, "fieldname": HIDDEN_FIELD}],
			}
		).insert(ignore_permissions=True)
		cls.rule = rule
		frappe.db.commit()
		clear_rule_cache()

	@classmethod
	def tearDownClass(cls):
		frappe.delete_doc("Report Field Rule", cls.rule.name, force=True, ignore_permissions=True)
		frappe.db.commit()
		clear_rule_cache()
		super().tearDownClass()

	def setUp(self):
		clear_rule_cache()

	def test_field_hidden_for_user_without_allowed_role(self):
		self.assertEqual(get_hidden_fieldnames(RULE_DOCTYPE, PLAIN_USER), {HIDDEN_FIELD})

	def test_field_visible_for_user_with_allowed_role(self):
		self.assertEqual(get_hidden_fieldnames(RULE_DOCTYPE, ALLOWED_USER), set())

	def test_administrator_is_never_restricted(self):
		self.assertEqual(get_hidden_fieldnames(RULE_DOCTYPE, "Administrator"), set())

	def test_unrelated_doctype_untouched(self):
		self.assertEqual(get_hidden_fieldnames("User", PLAIN_USER), set())

	def test_column_and_values_removed_for_dict_rows(self):
		result = strip_report_result(_sample_result(), RULE_DOCTYPE, PLAIN_USER)

		self.assertEqual([c["fieldname"] for c in result["columns"]], ["name", "status"])
		for row in result["result"]:
			self.assertNotIn(HIDDEN_FIELD, row)
			self.assertIn("status", row)
		# the value itself must be gone, not just the column header
		self.assertNotIn("secret", frappe.as_json(result["result"]))

	def test_positional_rows_are_stripped_by_index(self):
		result = _sample_result()
		result["result"] = [["ROW-1", "secret", "Open"], ["ROW-2", "also secret", "Closed"]]
		result = strip_report_result(result, RULE_DOCTYPE, PLAIN_USER)

		self.assertEqual(result["result"], [["ROW-1", "Open"], ["ROW-2", "Closed"]])

	def test_legacy_string_columns_are_matched(self):
		result = _sample_result()
		result["columns"] = ["ID:Data:120", "Disabled:Check:80", "Status:Data:100"]
		result["result"] = [["ROW-1", "secret", "Open"]]
		result = strip_report_result(result, RULE_DOCTYPE, PLAIN_USER)

		self.assertEqual(result["columns"], ["ID:Data:120", "Status:Data:100"])
		self.assertEqual(result["result"], [["ROW-1", "Open"]])

	def test_nothing_stripped_for_allowed_role(self):
		result = strip_report_result(_sample_result(), RULE_DOCTYPE, ALLOWED_USER)

		self.assertEqual(len(result["columns"]), 3)
		self.assertIn("secret", frappe.as_json(result["result"]))

	def test_chart_and_summary_referencing_hidden_field_are_dropped(self):
		result = _sample_result()
		result["chart"] = {"data": {"labels": ["a"], "datasets": [{"name": HIDDEN_FIELD}]}}
		result["report_summary"] = [
			{"label": "Total", "value": 10, "fieldname": "status"},
			{"label": "Leak", "value": 5, "fieldname": HIDDEN_FIELD},
		]
		result = strip_report_result(result, RULE_DOCTYPE, PLAIN_USER)

		self.assertIsNone(result["chart"])
		self.assertEqual([t["fieldname"] for t in result["report_summary"]], ["status"])

	def test_computed_report_column_is_accepted(self):
		"""Columns invented by a report have no DocField; they must still be hideable."""
		# A DocType with no real rule on it, so the test never collides with
		# configuration that exists on the site.
		probe_doctype = "ToDo"
		frappe.db.delete("Report Field Rule", {"reference_doctype": probe_doctype})
		rule = frappe.get_doc(
			{
				"doctype": "Report Field Rule",
				"reference_doctype": probe_doctype,
				"fields": [{"fieldname": "total_valuation"}],
			}
		).insert(ignore_permissions=True)
		try:
			row = rule.fields[0]
			self.assertEqual(row.fieldtype, "Report Column")
			self.assertEqual(row.source_doctype, probe_doctype)
			clear_rule_cache()
			self.assertIn("total_valuation", get_hidden_fieldnames(probe_doctype, PLAIN_USER))
		finally:
			frappe.delete_doc("Report Field Rule", rule.name, force=True, ignore_permissions=True)
			clear_rule_cache()

	def test_summary_tile_matched_by_label(self):
		"""A 'Total Valuation' tile must go when the total_valuation column goes."""
		result = {
			"columns": [
				{"fieldname": "vin_serial_no", "label": "VIN", "fieldtype": "Data"},
				{"fieldname": HIDDEN_FIELD, "label": "Total Valuation", "fieldtype": "Currency"},
			],
			"result": [frappe._dict({"vin_serial_no": "VIN1", HIDDEN_FIELD: 50000})],
			"report_summary": [
				{"label": "Total VINs", "value": 3, "datatype": "Int"},
				{"label": "Total Valuation", "value": 150000, "datatype": "Currency"},
			],
		}
		result = strip_report_result(result, RULE_DOCTYPE, PLAIN_USER)

		labels = [t["label"] for t in result["report_summary"]]
		self.assertEqual(labels, ["Total VINs"])
		self.assertNotIn("150000", frappe.as_json(result["report_summary"]))

	def test_add_column_endpoint_refuses_hidden_field(self):
		"""The Add Column data endpoint must not hand back a hidden field."""
		from frappe.desk import query_report

		from qcs_field_defaults.report_field_guard import install

		install()
		frappe.set_user(PLAIN_USER)
		try:
			with self.assertRaises(frappe.PermissionError):
				query_report.get_data_for_custom_field(RULE_DOCTYPE, HIDDEN_FIELD)
		finally:
			frappe.set_user("Administrator")

	def test_add_column_endpoint_allows_unrestricted_field(self):
		from frappe.desk import query_report

		from qcs_field_defaults.report_field_guard import install

		install()
		frappe.set_user(PLAIN_USER)
		try:
			# must not raise — only the ruled field is blocked
			query_report.get_data_for_custom_field(RULE_DOCTYPE, "reference_doctype")
		finally:
			frappe.set_user("Administrator")

	def test_get_report_columns_lists_saved_and_output_columns(self):
		from qcs_field_defaults.qcs_field_defaults.doctype.report_field_rule.report_field_rule import (
			get_report_columns,
		)

		report_name = "_Test Report Guard Columns"
		if frappe.db.exists("Report", report_name):
			frappe.delete_doc("Report", report_name, force=True, ignore_permissions=True)
		frappe.get_doc(
			{
				"doctype": "Report",
				"report_name": report_name,
				"ref_doctype": RULE_DOCTYPE,
				"report_type": "Query Report",
				"module": "Qcs Field Defaults",
				"is_standard": "No",
				"roles": [{"role": "System Manager"}],
				"query": "SELECT name AS 'name:Data:120', reference_doctype AS 'reference_doctype:Data:200' "
				"FROM `tabReport Field Rule` LIMIT 1",
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()
		try:
			payload = get_report_columns(report_name)
			fieldnames = {c["fieldname"] for c in payload["columns"]}
			self.assertIn("reference_doctype", fieldnames)
			self.assertIsNone(payload["error"])
			self.assertEqual(payload["ref_doctype"], RULE_DOCTYPE)
		finally:
			frappe.delete_doc("Report", report_name, force=True, ignore_permissions=True)
			frappe.db.commit()

	def test_rule_targeting_a_single_report(self):
		"""A rule with `report` set hides only in that report, not in its siblings."""
		report_a = "_Test Report Guard Target A"
		report_b = "_Test Report Guard Target B"
		for name in (report_a, report_b):
			if frappe.db.exists("Report", name):
				frappe.delete_doc("Report", name, force=True, ignore_permissions=True)
			frappe.get_doc(
				{
					"doctype": "Report",
					"report_name": name,
					"ref_doctype": "ToDo",
					"report_type": "Query Report",
					"module": "Qcs Field Defaults",
					"is_standard": "No",
					"roles": [{"role": "System Manager"}],
					"query": "SELECT name AS 'name:Data:120' FROM `tabToDo` LIMIT 1",
				}
			).insert(ignore_permissions=True)

		frappe.db.delete("Report Field Rule", {"report": report_a})
		rule = frappe.get_doc(
			{
				"doctype": "Report Field Rule",
				"report": report_a,
				"fields": [{"fieldname": "secret_total"}],
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()
		clear_rule_cache()
		try:
			self.assertEqual(rule.name, f"{report_a}-Report-Hidden")
			# reference_doctype is derived from the report
			self.assertEqual(rule.reference_doctype, "ToDo")

			# hidden in the targeted report...
			self.assertIn(
				"secret_total", get_hidden_fieldnames("ToDo", PLAIN_USER, report=report_a)
			)
			# ...but not in a sibling report on the same doctype
			self.assertNotIn(
				"secret_total", get_hidden_fieldnames("ToDo", PLAIN_USER, report=report_b)
			)
			# ...and not doctype-wide
			self.assertNotIn("secret_total", get_hidden_fieldnames("ToDo", PLAIN_USER))
		finally:
			frappe.delete_doc("Report Field Rule", rule.name, force=True, ignore_permissions=True)
			for name in (report_a, report_b):
				frappe.delete_doc("Report", name, force=True, ignore_permissions=True)
			frappe.db.commit()
			clear_rule_cache()

	def test_rule_needs_a_target(self):
		with self.assertRaises(frappe.ValidationError):
			frappe.get_doc(
				{"doctype": "Report Field Rule", "fields": [{"fieldname": "x"}]}
			).insert(ignore_permissions=True)

	def _make_query_report(self, name, query):
		if frappe.db.exists("Report", name):
			frappe.delete_doc("Report", name, force=True, ignore_permissions=True)
		frappe.get_doc(
			{
				"doctype": "Report",
				"report_name": name,
				"ref_doctype": RULE_DOCTYPE,
				"report_type": "Query Report",
				"module": "Qcs Field Defaults",
				"is_standard": "No",
				"roles": [{"role": "System Manager"}],
				"query": query,
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()

	def test_report_needing_company_filter_falls_back(self):
		"""A report that will not run bare must still yield its columns."""
		from qcs_field_defaults.qcs_field_defaults.doctype.report_field_rule.report_field_rule import (
			get_report_columns,
		)

		name = "_Test Report Guard Needs Company"
		self._make_query_report(
			name,
			"SELECT name AS 'name:Data:120' FROM `tabReport Field Rule` "
			"WHERE %(company)s IS NOT NULL LIMIT 1",
		)
		try:
			payload = get_report_columns(name)
			self.assertIn("name", {c["fieldname"] for c in payload["columns"]})
			self.assertIsNone(payload["error"])
			self.assertFalse(payload["needs_filters"])
			# it only succeeded because the fallback supplied a company
			self.assertIn("company", payload["filters_used"] or {})
		finally:
			frappe.delete_doc("Report", name, force=True, ignore_permissions=True)
			frappe.db.commit()

	def test_report_needing_unguessable_filter_reports_back(self):
		"""When the fallback cannot satisfy it, say so instead of failing silently."""
		from qcs_field_defaults.qcs_field_defaults.doctype.report_field_rule.report_field_rule import (
			get_report_columns,
		)

		name = "_Test Report Guard Needs Unknown"
		self._make_query_report(
			name,
			"SELECT name AS 'name:Data:120' FROM `tabReport Field Rule` "
			"WHERE %(unguessable_filter)s IS NOT NULL LIMIT 1",
		)
		try:
			payload = get_report_columns(name)
			self.assertEqual(payload["columns"], [])
			self.assertTrue(payload["needs_filters"])
			self.assertTrue(payload["error"])
		finally:
			frappe.delete_doc("Report", name, force=True, ignore_permissions=True)
			frappe.db.commit()

	def test_explicit_filters_are_used(self):
		from qcs_field_defaults.qcs_field_defaults.doctype.report_field_rule.report_field_rule import (
			get_report_columns,
		)

		name = "_Test Report Guard Explicit Filters"
		self._make_query_report(
			name,
			"SELECT name AS 'name:Data:120' FROM `tabReport Field Rule` "
			"WHERE %(unguessable_filter)s IS NOT NULL LIMIT 1",
		)
		try:
			payload = get_report_columns(name, filters={"unguessable_filter": "x"})
			self.assertIn("name", {c["fieldname"] for c in payload["columns"]})
			self.assertIsNone(payload["error"])
		finally:
			frappe.delete_doc("Report", name, force=True, ignore_permissions=True)
			frappe.db.commit()

	def test_disabled_rule_hides_nothing(self):
		frappe.db.set_value("Report Field Rule", self.rule.name, "disabled", 1)
		clear_rule_cache()
		try:
			self.assertEqual(get_hidden_fieldnames(RULE_DOCTYPE, PLAIN_USER), set())
		finally:
			frappe.db.set_value("Report Field Rule", self.rule.name, "disabled", 0)
			clear_rule_cache()

	def test_guard_is_installed_on_query_report_run(self):
		from qcs_field_defaults.report_field_guard import install

		install()
		from frappe.desk import query_report

		self.assertTrue(getattr(query_report.run, "__qcs_report_guard__", False))
		# installing twice must not double-wrap
		install()
		self.assertTrue(getattr(query_report.run, "__qcs_report_guard__", False))

	def test_end_to_end_query_report_is_stripped(self):
		"""Run a real Query Report through the patched entry point."""
		from frappe.desk import query_report

		from qcs_field_defaults.report_field_guard import install

		install()

		report_name = "_Test Report Guard Query"
		if frappe.db.exists("Report", report_name):
			frappe.delete_doc("Report", report_name, force=True, ignore_permissions=True)

		frappe.get_doc(
			{
				"doctype": "Report",
				"report_name": report_name,
				"ref_doctype": RULE_DOCTYPE,
				"report_type": "Query Report",
				"module": "Qcs Field Defaults",
				"is_standard": "No",
				"roles": [{"role": "System Manager"}],
				"query": f"SELECT name AS 'name:Data:120', {HIDDEN_FIELD} AS '{HIDDEN_FIELD}:Check:80' "
				"FROM `tabReport Field Rule` LIMIT 1",
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()

		try:
			frappe.set_user(PLAIN_USER)
			result = query_report.run(report_name, filters=None)
			fieldnames = {
				query_report.get_column_as_dict(c).get("fieldname") for c in (result.get("columns") or [])
			}
			self.assertNotIn(HIDDEN_FIELD, fieldnames)
			self.assertIn("name", fieldnames)

			frappe.set_user(ALLOWED_USER)
			clear_rule_cache()
			allowed = query_report.run(report_name, filters=None)
			allowed_fields = {
				query_report.get_column_as_dict(c).get("fieldname") for c in (allowed.get("columns") or [])
			}
			self.assertIn(HIDDEN_FIELD, allowed_fields)
		finally:
			frappe.set_user("Administrator")
			frappe.delete_doc("Report", report_name, force=True, ignore_permissions=True)
			frappe.db.commit()
