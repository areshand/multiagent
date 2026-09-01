import fs from "node:fs/promises";
import path from "node:path";
import { canonicalJson } from "./canonical-json.mjs";

export class ProjectionWorker {
  constructor({ store, directory, intervalMs, metrics }) {
    this.store = store;
    this.directory = directory;
    this.intervalMs = intervalMs;
    this.metrics = metrics;
    this.timer = null;
    this.running = false;
  }

  start() {
    if (!this.directory || this.timer) return;
    this.timer = setInterval(() => this.flush().catch(() => {}), this.intervalMs);
    this.timer.unref();
    void this.flush();
  }

  async flush() {
    if (!this.directory || this.running) return;
    this.running = true;
    try {
      await fs.mkdir(this.directory, { recursive: true, mode: 0o700 });
      for (const item of this.store.pendingProjections()) {
        try {
          const file = path.join(this.directory, `${item.entry.logId}.jsonl`);
          await fs.appendFile(file, `${canonicalJson(item.entry)}\n`, { encoding: "utf8", mode: 0o600 });
          this.store.markProjected(item.eventId);
          this.metrics.increment("audit_log_projection_success_total");
        } catch (error) {
          this.store.markProjectionFailed(item.eventId, item.attempts + 1, error.message);
          this.metrics.increment("audit_log_projection_failures_total");
        }
      }
    } finally {
      this.running = false;
    }
  }

  stop() {
    if (this.timer) clearInterval(this.timer);
    this.timer = null;
  }
}
