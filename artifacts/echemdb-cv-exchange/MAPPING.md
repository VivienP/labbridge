# LabBridge to EchemDB CV mapping

| LabBridge field | External field | Status | Origin/state | Semantic review | Loss or omission |
| --- | --- | --- | --- | --- | --- |
| `experiment.active_assertion_ids` | `—` | companion | — | not_applicable | Retained in the LabBridge companion manifest; no lossless EchemDB field exists. |
| `experiment.assertions` | `—` | companion | — | not_applicable | Retained in the LabBridge companion manifest; no lossless EchemDB field exists. |
| `experiment.assertions[exchange.curation.process]` | `/resources/0/metadata/echemdb/curation/process` | mapped | user_supplied/known | contract_checked | Copied from an explicit user assertion without semantic coercion. |
| `experiment.assertions[exchange.electrodes]` | `/resources/0/metadata/echemdb/system/electrodes` | mapped | user_supplied/known | fixture_declaration | Copied from an explicit user assertion without semantic coercion. |
| `experiment.assertions[exchange.electrolyte.type]` | `/resources/0/metadata/echemdb/system/electrolyte/type` | mapped | user_supplied/known | fixture_declaration | Copied from an explicit user assertion without semantic coercion. |
| `experiment.assertions[exchange.experimental]` | `/resources/0/metadata/echemdb/experimental` | mapped | user_supplied/known | contract_checked | Copied from an explicit user assertion without semantic coercion. |
| `experiment.assertions[exchange.measurement_type]` | `/resources/0/metadata/echemdb/figureDescription/measurementType` | mapped | user_supplied/known | fixture_declaration | Copied from an explicit user assertion without semantic coercion. |
| `experiment.assertions[exchange.source.citation_key]` | `/resources/0/metadata/echemdb/source/citationKey` | mapped | user_supplied/known | contract_checked | Copied from an explicit user assertion without semantic coercion. |
| `experiment.assertions[exchange.source.url]` | `/resources/0/metadata/echemdb/source/url` | mapped | user_supplied/known | contract_checked | Copied from an explicit user assertion without semantic coercion. |
| `experiment.assertions[exchange.system.type]` | `/resources/0/metadata/echemdb/system/type` | mapped | user_supplied/known | fixture_declaration | Copied from an explicit user assertion without semantic coercion. |
| `experiment.data_origin` | `—` | companion | — | not_applicable | Retained in the LabBridge companion manifest; no lossless EchemDB field exists. |
| `experiment.environment_id` | `—` | companion | — | not_applicable | Retained in the LabBridge companion manifest; no lossless EchemDB field exists. |
| `experiment.execution_mode` | `—` | companion | — | not_applicable | Retained in the LabBridge companion manifest; no lossless EchemDB field exists. |
| `experiment.experiment_id` | `—` | companion | — | not_applicable | Retained in the LabBridge companion manifest; no lossless EchemDB field exists. |
| `experiment.import_profile_id` | `—` | companion | — | not_applicable | Retained in the LabBridge companion manifest; no lossless EchemDB field exists. |
| `experiment.observation_id` | `—` | companion | — | not_applicable | Retained in the LabBridge companion manifest; no lossless EchemDB field exists. |
| `experiment.schema_version` | `—` | companion | — | not_applicable | Retained in the LabBridge companion manifest; no lossless EchemDB field exists. |
| `experiment.source_artifact_id` | `—` | companion | — | not_applicable | Retained in the LabBridge companion manifest; no lossless EchemDB field exists. |
| `experiment.supersedes_version` | `—` | companion | — | not_applicable | Retained in the LabBridge companion manifest; no lossless EchemDB field exists. |
| `experiment.technique` | `—` | companion | — | not_applicable | Retained in the LabBridge companion manifest; no lossless EchemDB field exists. |
| `experiment.transformation_ids` | `—` | companion | — | not_applicable | Retained in the LabBridge companion manifest; no lossless EchemDB field exists. |
| `experiment.version` | `—` | companion | — | not_applicable | Retained in the LabBridge companion manifest; no lossless EchemDB field exists. |
| `observation.data_origin` | `—` | companion | — | not_applicable | Retained in the LabBridge companion manifest; no lossless EchemDB field exists. |
| `observation.data_origin + observation.execution_mode` | `/resources/0/metadata/echemdb/figureDescription/type` | lossy | user_supplied/known | fixture_declaration | EchemDB figureDescription.type cannot represent LabBridge data_origin and execution_mode as independent dimensions. |
| `observation.environment_id` | `—` | companion | — | not_applicable | Retained in the LabBridge companion manifest; no lossless EchemDB field exists. |
| `observation.execution_mode` | `—` | companion | — | not_applicable | Retained in the LabBridge companion manifest; no lossless EchemDB field exists. |
| `observation.import_profile_id` | `—` | companion | — | not_applicable | Retained in the LabBridge companion manifest; no lossless EchemDB field exists. |
| `observation.metadata` | `—` | companion | — | not_applicable | Retained in the LabBridge companion manifest; no lossless EchemDB field exists. |
| `observation.metadata.contact_area` | `—` | omitted | unknown | not_applicable | Unknown or unsupported metadata is not exported. |
| `observation.metadata.current_basis` | `—` | omitted | unknown | not_applicable | Unknown or unsupported metadata is not exported. |
| `observation.metadata.cycle_information` | `—` | omitted | unknown | not_applicable | Unknown or unsupported metadata is not exported. |
| `observation.metadata.electrode_role` | `—` | omitted | unknown | not_applicable | Unknown or unsupported metadata is not exported. |
| `observation.metadata.geometric_area` | `—` | omitted | unknown | not_applicable | Unknown or unsupported metadata is not exported. |
| `observation.metadata.potential_treatment` | `—` | omitted | unknown | not_applicable | Unknown or unsupported metadata is not exported. |
| `observation.metadata.reference_scale` | `—` | omitted | unknown | not_applicable | Unknown or unsupported metadata is not exported. |
| `observation.metadata.scan_rate` | `—` | omitted | unknown | not_applicable | Unknown or unsupported metadata is not exported. |
| `observation.normalisation_version` | `—` | companion | — | not_applicable | Retained in the LabBridge companion manifest; no lossless EchemDB field exists. |
| `observation.observation_id` | `—` | companion | — | not_applicable | Retained in the LabBridge companion manifest; no lossless EchemDB field exists. |
| `observation.parser_record_id` | `—` | companion | — | not_applicable | Retained in the LabBridge companion manifest; no lossless EchemDB field exists. |
| `observation.parser_version` | `—` | companion | — | not_applicable | Retained in the LabBridge companion manifest; no lossless EchemDB field exists. |
| `observation.provenance` | `—` | companion | — | not_applicable | Retained in the LabBridge companion manifest; no lossless EchemDB field exists. |
| `observation.row_count` | `—` | companion | — | not_applicable | Retained in the LabBridge companion manifest; no lossless EchemDB field exists. |
| `observation.schema_version` | `—` | companion | — | not_applicable | Retained in the LabBridge companion manifest; no lossless EchemDB field exists. |
| `observation.series` | `—` | companion | — | not_applicable | Retained in the LabBridge companion manifest; no lossless EchemDB field exists. |
| `observation.series[].dtype` | `—` | companion | — | not_applicable | Retained in the LabBridge companion manifest; no lossless EchemDB field exists. |
| `observation.series[].role` | `—` | companion | — | not_applicable | Retained in the LabBridge companion manifest; no lossless EchemDB field exists. |
| `observation.series[].schema_version` | `—` | companion | — | not_applicable | Retained in the LabBridge companion manifest; no lossless EchemDB field exists. |
| `observation.series[].series_id` | `—` | companion | — | not_applicable | Retained in the LabBridge companion manifest; no lossless EchemDB field exists. |
| `observation.series[].shape` | `—` | companion | — | not_applicable | Retained in the LabBridge companion manifest; no lossless EchemDB field exists. |
| `observation.series[].source_column` | `/resources/0/schema/fields/*/name` | mapped | — | not_applicable | Copied without numeric conversion or semantic reinterpretation. |
| `observation.series[].source_unit` | `—` | companion | — | not_applicable | Retained in the LabBridge companion manifest; no lossless EchemDB field exists. |
| `observation.series[].transformation_id` | `—` | companion | — | not_applicable | Retained in the LabBridge companion manifest; no lossless EchemDB field exists. |
| `observation.series[].unit` | `/resources/0/schema/fields/*/unit` | mapped | — | not_applicable | Copied without numeric conversion or semantic reinterpretation. |
| `observation.series[].values` | `/resources/0/data/rows/*` | mapped | — | not_applicable | Copied without numeric conversion or semantic reinterpretation. |
| `observation.source_artifact_id` | `—` | companion | — | not_applicable | Retained in the LabBridge companion manifest; no lossless EchemDB field exists. |
| `observation.transformation_ids` | `—` | companion | — | not_applicable | Retained in the LabBridge companion manifest; no lossless EchemDB field exists. |

The machine-readable authority is `mapping.json`; `mapping.csv` contains the same rows.
