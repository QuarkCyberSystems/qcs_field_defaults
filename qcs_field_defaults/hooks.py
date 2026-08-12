app_name = "qcs_field_defaults"
app_title = "Qcs Field Defaults"
app_publisher = "QCS"
app_description = "Tool to set defaults on fields"
app_email = "info@quarkcs.com"
app_license = "mit"

required_apps = ["frappe"]

app_include_js = "/assets/qcs_field_defaults/js/qcs_field_defaults.js"

extend_bootinfo = "qcs_field_defaults.utils.extend_bootinfo"

doc_events = {
	"*": {
		"before_insert": "qcs_field_defaults.utils.apply_field_defaults",
	}
}

# Report Field Rule — strip restricted columns out of Query/Script Report output.
# Installed per request/job because the export path calls the module-level
# `run`, which an override_whitelisted_methods entry would not intercept.
before_request = ["qcs_field_defaults.report_field_guard.install"]
before_job = ["qcs_field_defaults.report_field_guard.install"]
