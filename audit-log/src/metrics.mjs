export class Metrics {
  constructor() {
    this.values = new Map([
      ["audit_log_appends_total", 0],
      ["audit_log_duplicate_appends_total", 0],
      ["audit_log_rejected_requests_total", 0],
      ["audit_log_projection_success_total", 0],
      ["audit_log_projection_failures_total", 0],
    ]);
    this.integrity = 1;
  }

  increment(name) {
    this.values.set(name, (this.values.get(name) || 0) + 1);
  }

  render(projectionCounts = {}) {
    const lines = [
      "# HELP audit_log_integrity_ok Whether the last full ledger verification succeeded.",
      "# TYPE audit_log_integrity_ok gauge",
      `audit_log_integrity_ok ${this.integrity}`,
    ];
    for (const [name, value] of this.values) {
      lines.push(`# TYPE ${name} counter`, `${name} ${value}`);
    }
    for (const status of ["pending", "complete"]) {
      lines.push(`audit_log_projection_queue{status="${status}"} ${projectionCounts[status] || 0}`);
    }
    return `${lines.join("\n")}\n`;
  }
}
