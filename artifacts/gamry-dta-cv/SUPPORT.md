# Supported Gamry DTA CV variant

The bounded parser accepts UTF-8 or declared UTF-8 BOM text containing `EXPLAIN`, `TAG	CV`, one
`FRAMEWORKVERSION	QUANT	7.07` object, and exactly one `CURVE	TABLE` object. The declared table
must contain these columns and units in order:

| Column | Source unit | Explicit role |
| --- | --- | --- |
| `Pt` | `#` | ignored |
| `T` | `s` | time in `s` |
| `Vf` | `V vs. Ref.` | potential in `V` |
| `Im` | `A` | current in `A` |
| `Vu`, `Sig`, `Ach`, `IERange`, `Over`, `Temp` | declared by DTA | ignored |
| `Cycle` | `#` | cycle index in `1` |

Decimal point and decimal comma are supported only when the import profile declares the matching
convention. The parser verifies the declared row count and records exact header, unit, and data-line
locations for every accepted scientific field. `V vs. Ref.` is retained as the source unit; the
reference scale remains `unknown` and no electrochemical convention is inferred.

## Parser decision

`echemdb-converters` 0.4.1 was evaluated at source-code level. Its Gamry loader locates a first
`CURVE` table and delegates table parsing, but it does not provide the bounded variant validation,
mixed-block rejection, durable diagnostics, or field-level line provenance required here. LabBridge
therefore uses a small in-repository parser and adds no runtime dependency. References:

- https://github.com/echemdb/echemdb-converters/blob/main/echemdbconverters/gamryloader.py
- https://help.gamry.com/Framework/general-information_datafileformat.html
