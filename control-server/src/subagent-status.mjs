import fs from "node:fs";
import path from "node:path";

const terminalAgentStatuses = new Set(["done", "completed", "closed", "cancelled", "canceled", "failed", "released", "skipped", "finalized", "killed", "missing"]);

function readStatusFile(file, maxBytes = 128 * 1024) {
  try {
    const value = fs.readFileSync(file);
    return value.subarray(Math.max(0, value.length - maxBytes)).toString("utf8");
  } catch {
    return "";
  }
}

function parseStatusEnv(text) {
  return Object.fromEntries(String(text || "").split("\n").map((line) => {
    const split = line.indexOf("=");
    return split > 0 ? [line.slice(0, split), line.slice(split + 1)] : null;
  }).filter(Boolean));
}

function boundedStatusText(value, max = 180) {
  return String(value || "")
    .replace(/\u001b\[[0-?]*[ -/]*[@-~]/g, "")
    .replace(/[\r\n\t]+/g, " ")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, max);
}

function lastProgressLine(text) {
  return String(text || "").split(/\r?\n/).reverse().map((line) => boundedStatusText(line)).find((line) => {
    if (!line || /^[─━│┃╭╮╰╯┌┐└┘═]+$/.test(line)) return false;
    if (new Set([">", "❯", "Working…", "Working..."]).has(line)) return false;
    if (/^(?:final status:|Multiagent launch mode:)/i.test(line)) return false;
    if (/[{,]\s*\\?"(?:type|session_id|uuid|usage|duration_ms)\\?"\s*:/.test(line)) return false;
    try { if (typeof JSON.parse(line) === "object") return false; } catch {}
    if (/[{}\[\]`]|\\[nrt"]|"\s*:|\bsignature\b/i.test(line)) return false;
    return /^(?:Analyzing|Checking|Collecting|Comparing|Executing|Finding|Found|Inspecting|Investigating|Preparing|Querying|Reading|Reviewing|Running|Summarizing|Tracing|Validating|Waiting|Working)\b/.test(line);
  }) || "";
}

function assignmentSummary(text) {
  const value = String(text || "");
  const taskAssignment = value.match(/(?:^|\n)## Task Assignment\s*\n+([\s\S]*?)(?=\n## |$)/)?.[1];
  if (taskAssignment?.trim()) return boundedStatusText(taskAssignment);
  const marker = value.lastIndexOf("----- BEGIN TASK APPENDIX");
  const section = marker >= 0 ? value.slice(marker) : value;
  const heading = section.split(/\r?\n/).map((line) => line.match(/^#{1,3}\s+(.+)/)?.[1] || "").find(Boolean);
  return boundedStatusText(heading || section.split(/\r?\n/).find((line) => line.trim()) || "");
}

export function readSubagentSnapshot(sessionRoot) {
  const root = path.join(sessionRoot, "subagents");
  let entries;
  try {
    entries = fs.readdirSync(root, { withFileTypes: true }).filter((entry) => entry.isDirectory()).slice(0, 64);
  } catch {
    return [];
  }
  const agents = entries.map((entry) => {
    const directory = path.join(root, entry.name);
    const status = boundedStatusText(readStatusFile(path.join(directory, "status")), 32) || "unknown";
    const metadata = parseStatusEnv(readStatusFile(path.join(directory, "meta.env")));
    const assignment = parseStatusEnv(readStatusFile(path.join(sessionRoot, "assignments", entry.name, "assignment.env")));
    const summary = assignmentSummary(readStatusFile(path.join(directory, "instruction.txt")));
    const progress = lastProgressLine(readStatusFile(path.join(directory, "current.txt")));
    let updatedAt = null;
    try { updatedAt = fs.statSync(path.join(directory, "status")).mtime.toISOString(); } catch {}
    return {
      name: boundedStatusText(entry.name, 64),
      status,
      role: boundedStatusText(metadata.role || assignment.role || "", 48),
      workingOn: terminalAgentStatuses.has(status.toLowerCase()) ? summary || progress : progress || summary,
      assignment: summary,
      updatedAt,
    };
  });
  return agents.sort((left, right) => {
    const terminalDifference = Number(terminalAgentStatuses.has(left.status)) - Number(terminalAgentStatuses.has(right.status));
    return terminalDifference || left.name.localeCompare(right.name);
  });
}
