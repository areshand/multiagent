export async function visibleLegacySessionIds({ records, username, hasThread }) {
  const ids = Object.keys(records).sort().filter((id) => records[id]?.createdBy === username);
  const visible = [];
  for (const id of ids) {
    if (!await hasThread(records[id]?.threadId, username)) visible.push(id);
  }
  return visible;
}
