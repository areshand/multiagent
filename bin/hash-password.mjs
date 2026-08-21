#!/usr/bin/env node
import crypto from "node:crypto";
import readline from "node:readline";

const username = process.argv[2];
if (!username || !/^[a-zA-Z0-9._-]{1,64}$/.test(username)) {
  console.error("usage: bin/hash-password.mjs USERNAME");
  process.exit(2);
}
const terminal = readline.createInterface({ input: process.stdin, output: process.stderr, terminal: true });
terminal.question("Password: ", (password) => {
  terminal.close();
  if (password.length < 12) { console.error("password must be at least 12 characters"); process.exit(2); }
  const salt = crypto.randomBytes(16);
  const hash = crypto.scryptSync(password, salt, 64, { N: 16384, r: 8, p: 1, maxmem: 64 * 1024 * 1024 });
  console.log(JSON.stringify({ username, passwordHash: `scrypt$16384$8$1$${salt.toString("base64url")}$${hash.toString("base64url")}` }));
});
