import crypto from "node:crypto";
import fs from "node:fs/promises";
import path from "node:path";

function eventFileName(eventId) {
  return `${crypto.createHash("sha256").update(String(eventId)).digest("hex")}.json`;
}

export class SlackEventFileQueue {
  constructor(root) {
    this.root = path.resolve(root);
    this.pending = path.join(this.root, "pending");
    this.draining = null;
  }

  async initialize() {
    await fs.mkdir(this.pending, { recursive: true, mode: 0o700 });
  }

  async enqueue(event) {
    await this.initialize();
    const file = path.join(this.pending, eventFileName(event.eventId));
    const temporary = path.join(this.pending, `.tmp-${process.pid}-${crypto.randomUUID()}`);
    await fs.writeFile(temporary, JSON.stringify(event) + "\n", { mode: 0o600, flag: "wx" });
    try {
      await fs.link(temporary, file);
      return { queued: true, duplicate: false };
    } catch (error) {
      if (error?.code !== "EEXIST") throw error;
      return { queued: false, duplicate: true };
    } finally {
      await fs.unlink(temporary).catch(() => {});
    }
  }

  async size() {
    await this.initialize();
    return (await fs.readdir(this.pending)).filter((name) => name.endsWith(".json") && !name.startsWith(".")).length;
  }

  async drainOne(deliver) {
    if (this.draining) return this.draining;
    this.draining = this.#drainOne(deliver).finally(() => { this.draining = null; });
    return this.draining;
  }

  async #drainOne(deliver) {
    await this.initialize();
    const names = (await fs.readdir(this.pending)).filter((name) => /^[a-f0-9]{64}\.json$/.test(name)).sort();
    const name = names[0];
    if (!name) return { delivered: false, empty: true };
    const file = path.join(this.pending, name);
    let event;
    try { event = JSON.parse(await fs.readFile(file, "utf8")); }
    catch (error) { return { delivered: false, empty: false, error }; }
    try {
      await deliver(event);
      await fs.unlink(file);
      return { delivered: true, empty: false };
    } catch (error) {
      return { delivered: false, empty: false, error };
    }
  }
}
