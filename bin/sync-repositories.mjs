#!/usr/bin/env node
import fs from "node:fs";
import path from "node:path";
import { execFileSync } from "node:child_process";

const configFile = process.env.MULTIAGENT_REPOSITORIES_FILE || "/etc/multiagent/repositories.json";
const root = path.resolve(process.env.MULTIAGENT_REPOSITORY_ROOT || "/var/lib/multiagent/repositories");
if (!fs.existsSync(configFile)) process.exit(0);
const config = JSON.parse(fs.readFileSync(configFile, "utf8"));
fs.mkdirSync(root, { recursive: true });
for (const repository of config.repositories || []) {
  if (!/^[a-z0-9][a-z0-9-]{0,62}$/.test(repository.name)) throw new Error(`invalid repository name: ${repository.name}`);
  if (!/^https:\/\/github\.com\/[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+(?:\.git)?$/.test(repository.url)) throw new Error(`unsupported repository URL: ${repository.url}`);
  const destination = path.join(root, repository.name);
  if (!fs.existsSync(path.join(destination, ".git"))) {
    execFileSync("git", ["clone", "--origin", "origin", repository.url, destination], { stdio: "inherit" });
  } else {
    execFileSync("git", ["-C", destination, "fetch", "origin", "--prune"], { stdio: "inherit" });
  }
  const status = execFileSync("git", ["-C", destination, "status", "--porcelain"], { encoding: "utf8" });
  if (!status.trim() && repository.ref) {
    execFileSync("git", ["-C", destination, "checkout", repository.ref], { stdio: "inherit" });
    execFileSync("git", ["-C", destination, "pull", "--ff-only", "origin", repository.ref], { stdio: "inherit" });
  }
}
