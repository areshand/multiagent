#!/usr/bin/env node
import crypto from "node:crypto";

const username = process.argv[2];
if (!username || !/^[a-zA-Z0-9._-]{1,64}$/.test(username)) {
  console.error("usage: bin/hash-password.mjs USERNAME");
  process.exit(2);
}

// Echoing the password would leave it in scrollback, shell history, and any
// captured terminal log, so the prompt reads raw keystrokes instead.
function readPassword(prompt) {
  return new Promise((resolve, reject) => {
    const input = process.stdin;
    if (!input.isTTY) {
      reject(new Error("stdin must be a terminal"));
      return;
    }
    process.stderr.write(prompt);
    input.setRawMode(true);
    input.resume();
    input.setEncoding("utf8");
    let value = "";
    const settle = (error, result) => {
      input.removeListener("data", onData);
      input.setRawMode(false);
      input.pause();
      process.stderr.write("\n");
      if (error) reject(error);
      else resolve(result);
    };
    const onData = (chunk) => {
      for (const character of chunk) {
        if (character === "\r" || character === "\n" || character === "\u0004") return settle(null, value);
        if (character === "\u0003") return settle(new Error("cancelled"));
        if (character === "\u007f" || character === "\b") value = value.slice(0, -1);
        else if (character >= " ") value += character;
      }
    };
    input.on("data", onData);
  });
}

const password = await readPassword("Password: ");
if (password.length < 12) { console.error("password must be at least 12 characters"); process.exit(2); }
const salt = crypto.randomBytes(16);
const hash = crypto.scryptSync(password, salt, 64, { N: 16384, r: 8, p: 1, maxmem: 64 * 1024 * 1024 });
console.log(JSON.stringify({ username, passwordHash: `scrypt$16384$8$1$${salt.toString("base64url")}$${hash.toString("base64url")}` }));
