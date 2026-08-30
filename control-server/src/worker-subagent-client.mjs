import http from "node:http";

export function fetchWorkerSubagents({
  sessionId,
  hostname,
  port = 8080,
  token,
  timeoutMs = 1500,
  maxBytes = 128 * 1024,
}) {
  return new Promise((resolve, reject) => {
    const request = http.request({
      hostname,
      port,
      method: "GET",
      path: `/api/sessions/${sessionId}/agents`,
      headers: { accept: "application/json", authorization: `Bearer ${token}` },
      timeout: timeoutMs,
    }, (response) => {
      const chunks = [];
      let size = 0;
      response.on("data", (chunk) => {
        size += chunk.length;
        if (size > maxBytes) response.destroy(new Error("session subagent snapshot exceeds limit"));
        else chunks.push(chunk);
      });
      response.on("end", () => {
        if (response.statusCode < 200 || response.statusCode >= 300) return reject(new Error(`session worker subagent snapshot returned ${response.statusCode}`));
        try {
          const value = JSON.parse(Buffer.concat(chunks).toString("utf8"));
          if (!Array.isArray(value?.agents)) throw new Error("session worker returned invalid subagent snapshot");
          resolve(value.agents);
        } catch (error) { reject(error); }
      });
    });
    request.on("timeout", () => request.destroy(new Error("session worker subagent snapshot timed out")));
    request.on("error", reject);
    request.end();
  });
}

export async function fetchWorkerSubagentsWithReconciliation({
  record,
  fetchSnapshot,
  reconcile,
}) {
  const initialPodIP = record?.podIP || null;
  try {
    return { agents: await fetchSnapshot(initialPodIP), record, error: null };
  } catch (initialError) {
    let refreshed;
    try {
      refreshed = await reconcile();
    } catch {
      return { agents: null, record, error: initialError };
    }
    if (refreshed?.status !== "running" || !refreshed.podIP) {
      return { agents: null, record: refreshed, error: null };
    }
    if (refreshed.podIP === initialPodIP) {
      return { agents: null, record: refreshed, error: initialError };
    }
    try {
      return { agents: await fetchSnapshot(refreshed.podIP), record: refreshed, error: null };
    } catch (retryError) {
      return { agents: null, record: refreshed, error: retryError };
    }
  }
}
