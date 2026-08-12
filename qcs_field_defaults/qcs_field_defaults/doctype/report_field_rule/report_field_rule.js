// Copyright (c) 2026, QCS and contributors
// For license information, please see license.txt

frappe.ui.form.on("Report Field Rule", {
	refresh(frm) {
		add_buttons(frm);
		frm.set_intro(
			__(
				"Columns listed here are removed from Query and Script Report responses on the server, including CSV/Excel exports. List View, Report View and the REST API are covered separately by Perm Level Rule."
			),
			"blue"
		);
	},

	reference_doctype(frm) {
		frm.clear_table("fields");
		frm.refresh_field("fields");
		add_buttons(frm);
	},

	report(frm) {
		// A report-targeted rule derives its DocType from the report, so the field
		// pickers still have something to work from.
		if (frm.doc.report) {
			frappe.db.get_value("Report", frm.doc.report, "ref_doctype").then((r) => {
				if (r.message && r.message.ref_doctype) {
					frm.set_value("reference_doctype", r.message.ref_doctype);
				}
				add_buttons(frm);
			});
		} else {
			add_buttons(frm);
		}
	},
});

function add_buttons(frm) {
	frm.remove_custom_button(__("Select Fields"));
	frm.remove_custom_button(__("Select From Report"));
	frm.remove_custom_button(__("Preview For User"));

	if (!frm.doc.reference_doctype) return;

	frm.add_custom_button(__("Select Fields"), () => show_field_selector(frm));
	frm.add_custom_button(__("Select From Report"), () => show_report_column_selector(frm));

	if (!frm.is_new()) {
		frm.add_custom_button(__("Preview For User"), () => show_preview(frm));
	}
}

function show_report_column_selector(frm, prefill) {
	// Picks from what a report actually emits — computed columns and "Add Column"
	// columns included — rather than from the DocType's stored fields.
	prefill = prefill || {};
	const picker = new frappe.ui.Dialog({
		title: __("Pick A Report"),
		fields: [
			{
				fieldtype: "Link",
				fieldname: "report",
				label: __("Report"),
				options: "Report",
				reqd: 1,
				default: prefill.report || frm.doc.report,
				get_query: () => ({ filters: { ref_doctype: frm.doc.reference_doctype, disabled: 0 } }),
			},
			{
				fieldtype: "Section Break",
				label: __("Filters"),
				collapsible: 1,
				collapsible_depends_on: "eval:true",
			},
			{
				fieldtype: "Code",
				fieldname: "filters",
				label: __("Report Filters (JSON)"),
				options: "JSON",
				default: prefill.filters || "",
				description: __(
					"Only needed when the report refuses to run without them, e.g. {\"company\": \"My Company\"}. Leave empty to try the report's defaults."
				),
			},
		],
		primary_action_label: __("Load Columns"),
		primary_action(values) {
			picker.hide();
			frappe.call({
				method: "qcs_field_defaults.qcs_field_defaults.doctype.report_field_rule.report_field_rule.get_report_columns",
				args: { report_name: values.report, filters: values.filters || null },
				freeze: true,
				freeze_message: __("Reading report columns..."),
				callback(r) {
					const payload = r.message || {};
					const columns = payload.columns || [];

					if (!columns.length) {
						// Mandatory filters are declared in the report's client script, so
						// the server cannot guess them all — ask for them and retry.
						frappe.msgprint({
							title: __("Report needs filters"),
							message: `${__("The report could not be run, so its columns are unknown.")}<br><br><b>${
								__("Report said:")
							}</b> ${frappe.utils.escape_html(payload.error || "")}<br><br>${__(
								"Enter the required filters as JSON and try again."
							)}`,
							indicator: "orange",
						});
						show_report_column_selector(frm, {
							report: values.report,
							filters:
								values.filters ||
								JSON.stringify(payload.filters_used || { company: frappe.defaults.get_user_default("Company") }, null, 1),
						});
						return;
					}

					const existing = new Set((frm.doc.fields || []).map((row) => row.fieldname));
					const options = columns.map((c) => ({
						label: `${c.label} (${c.fieldname}) [${c.source}]`,
						value: c.fieldname,
						checked: existing.has(c.fieldname),
					}));

					const d = new frappe.ui.Dialog({
						title: __("Columns In {0}", [values.report]),
						size: "large",
						fields: [
							...(payload.error
								? [
										{
											fieldtype: "HTML",
											options: `<div class="alert alert-warning" style="font-size:12px">${__(
												"The report could not be executed, so only its saved columns are listed."
											)}<br><code>${frappe.utils.escape_html(payload.error).slice(0, 300)}</code></div>`,
										},
								  ]
								: []),
							{ fieldtype: "MultiCheck", fieldname: "columns", options: options, columns: 2 },
						],
						primary_action_label: __("Add To Rule"),
						primary_action(vals) {
							const chosen = vals.columns || [];
							for (const fieldname of chosen) {
								if (existing.has(fieldname)) continue;
								const col = columns.find((c) => c.fieldname === fieldname);
								const row = frm.add_child("fields");
								row.source_doctype = frm.doc.reference_doctype;
								row.fieldname = fieldname;
								row.label = col?.label || fieldname;
							}
							frm.refresh_field("fields");
							frm.dirty();
							d.hide();
						},
					});
					d.show();
				},
			});
		},
	});
	picker.show();
}

function show_preview(frm) {
	const d = new frappe.ui.Dialog({
		title: __("Hidden Fields For A User"),
		fields: [{ fieldtype: "Link", fieldname: "user", label: __("User"), options: "User", reqd: 1 }],
		primary_action_label: __("Check"),
		primary_action(values) {
			frappe.call({
				method: "qcs_field_defaults.qcs_field_defaults.doctype.report_field_rule.report_field_rule.preview_rule",
				args: {
					reference_doctype: frm.doc.reference_doctype,
					user: values.user,
					report: frm.doc.report,
				},
				callback(r) {
					const payload = r.message || {};
					const rows = payload.fields || [];
					const reports = payload.reports || [];

					const fields_html = rows.length
						? `<ul>${rows
								.map(
									(f) =>
										`<li>${frappe.utils.escape_html(f.label)} <code>${frappe.utils.escape_html(
											f.fieldname
										)}</code> <span class="text-muted">— ${frappe.utils.escape_html(f.source)}</span></li>`
								)
								.join("")}</ul>`
						: `<p>${__("This user sees every field.")}</p>`;

					// Reports are the thing people get wrong: a rule only applies to
					// reports whose Reference DocType matches this one.
					const reports_html = reports.length
						? `<ul>${reports
								.map(
									(rep) =>
										`<li>${frappe.utils.escape_html(rep.name)} <span class="text-muted">(${frappe.utils.escape_html(
											rep.report_type
										)})</span></li>`
								)
								.join("")}</ul>`
						: `<p class="text-danger">${__(
								"No report uses this DocType as its Reference DocType, so this rule currently hides nothing."
						  )}</p>`;

					frappe.msgprint({
						title: __("Hidden in reports for {0}", [values.user]),
						message: `<h5>${__("Fields hidden")}</h5>${fields_html}<h5 class="mt-3">${__(
							"Reports covered by this rule"
						)}</h5>${reports_html}`,
						indicator: rows.length ? "orange" : "green",
					});
					d.hide();
				},
			});
		},
	});
	d.show();
}

function show_field_selector(frm) {
	const parent_dt = frm.doc.reference_doctype;

	const existing = new Set(
		(frm.doc.fields || []).map((r) => `${r.source_doctype}|${r.fieldname}`)
	);

	frappe.call({
		// Reuses the selector endpoint already shipped for Perm Level Rule.
		method: "qcs_field_defaults.qcs_field_defaults.doctype.perm_level_rule.perm_level_rule.get_selectable_fields",
		args: { reference_doctype: parent_dt },
		callback(r) {
			const payload = r.message || {};
			const doctypes_to_load = payload.doctypes_to_load || [];
			const fields_by_doctype = payload.fields_by_doctype || {};
			const dialog_fields = [];

			for (const dt of doctypes_to_load) {
				const data_fields = fields_by_doctype[dt] || [];
				if (!data_fields.length) continue;

				const options = data_fields.map((f) => ({
					label: `${f.label || f.fieldname} (${f.fieldname}) [${f.fieldtype}]`,
					value: f.fieldname,
					checked: existing.has(`${dt}|${f.fieldname}`),
				}));

				dialog_fields.push({
					fieldtype: "HTML",
					options: `<h5 class="mt-3 mb-2">${dt === parent_dt ? dt : `${dt} (child table)`}</h5>`,
				});
				dialog_fields.push({
					fieldtype: "MultiCheck",
					fieldname: dt,
					options: options,
					columns: 2,
				});
			}

			const d = new frappe.ui.Dialog({
				title: __("Select Fields To Hide In Reports For {0}", [parent_dt]),
				fields: dialog_fields,
				size: "extra-large",
				primary_action_label: __("Apply"),
				primary_action(values) {
					frm.clear_table("fields");

					for (const dt of doctypes_to_load) {
						const selected = values[dt] || [];
						const field_rows = fields_by_doctype[dt] || [];

						for (const fieldname of selected) {
							const df = field_rows.find((f) => f.fieldname === fieldname);
							const row = frm.add_child("fields");
							row.source_doctype = dt;
							row.fieldname = fieldname;
							row.label = df?.label || fieldname;
							row.fieldtype = df?.fieldtype || "";
						}
					}

					frm.refresh_field("fields");
					frm.dirty();
					d.hide();
				},
			});
			d.show();
		},
	});
}
