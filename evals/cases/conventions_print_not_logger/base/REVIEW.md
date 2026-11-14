# Review rules

## Standard
Report defects and broken rules below. Style a formatter fixes is not a finding.

## Extra checks
- Production code logs through `app.log.get_logger()`, never `print`: the
  service's output is collected as structured JSON and a bare print line breaks it.
