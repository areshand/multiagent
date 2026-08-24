import fs from "node:fs";
import https from "node:https";

const placeholderPattern = /\{\{([A-Z][A-Z0-9_]*)\}\}/g;

export function renderSessionTemplate(value, replacements) {
  if (Array.isArray(value)) return value.map((item) => renderSessionTemplate(item, replacements));
  if (value && typeof value === "object") {
    return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, renderSessionTemplate(item, replacements)]));
  }
  if (typeof value !== "string") return value;
  const rendered = value.replace(placeholderPattern, (_, name) => {
    if (!(name in replacements)) throw new Error(`session Job template uses unknown placeholder: ${name}`);
    return String(replacements[name]);
  });
  if (rendered.includes("{{") || rendered.includes("}}")) throw new Error("session Job template contains an invalid placeholder");
  return rendered;
}

export function sessionSecret(id, namespace, task, actor) {
  return {
    apiVersion: "v1",
    kind: "Secret",
    metadata: {
      name: `multiagent-session-${id}`,
      namespace,
      labels: {
        "app.kubernetes.io/name": "multiagent-session",
        "multiagent.movement.io/session": id,
      },
    },
    immutable: true,
    type: "Opaque",
    data: {
      "task.md": Buffer.from(task, "utf8").toString("base64"),
      actor: Buffer.from(actor, "utf8").toString("base64"),
    },
  };
}

export function jobPhase(job) {
  if (!job) return "pending";
  if (Number(job.status?.succeeded || 0) > 0) return "completed";
  if (Number(job.status?.failed || 0) > 0) return "failed";
  if (Number(job.status?.active || 0) > 0) return "running";
  return "pending";
}

function apiRequest({ host, port, token, ca }, method, requestPath, body) {
  return new Promise((resolve, reject) => {
    const encoded = body === undefined ? null : Buffer.from(JSON.stringify(body));
    const request = https.request({
      hostname: host,
      port,
      method,
      path: requestPath,
      ca,
      headers: {
        authorization: `Bearer ${token}`,
        accept: "application/json",
        ...(encoded ? { "content-type": "application/json", "content-length": encoded.length } : {}),
      },
      timeout: 15_000,
    }, (response) => {
      const chunks = [];
      response.on("data", (chunk) => chunks.push(chunk));
      response.on("end", () => {
        const text = Buffer.concat(chunks).toString("utf8");
        let parsed = null;
        try { parsed = text ? JSON.parse(text) : null; } catch {}
        if (response.statusCode >= 200 && response.statusCode < 300) return resolve(parsed);
        const error = new Error(parsed?.message || `Kubernetes API ${method} ${requestPath} returned ${response.statusCode}`);
        error.statusCode = response.statusCode;
        reject(error);
      });
    });
    request.on("timeout", () => request.destroy(new Error("Kubernetes API request timed out")));
    request.on("error", reject);
    if (encoded) request.write(encoded);
    request.end();
  });
}

export class KubernetesSessionClient {
  constructor(options = {}) {
    const serviceAccountRoot = options.serviceAccountRoot || "/var/run/secrets/kubernetes.io/serviceaccount";
    this.namespace = options.namespace || process.env.MULTIAGENT_SESSION_NAMESPACE
      || fs.readFileSync(`${serviceAccountRoot}/namespace`, "utf8").trim();
    this.connection = {
      host: options.host || process.env.KUBERNETES_SERVICE_HOST,
      port: Number(options.port || process.env.KUBERNETES_SERVICE_PORT_HTTPS || "443"),
      token: options.token || fs.readFileSync(`${serviceAccountRoot}/token`, "utf8").trim(),
      ca: options.ca || fs.readFileSync(`${serviceAccountRoot}/ca.crt`),
    };
    if (!this.connection.host) throw new Error("KUBERNETES_SERVICE_HOST is required in gateway mode");
  }

  path(resource, name = "") {
    return `/apis/batch/v1/namespaces/${encodeURIComponent(this.namespace)}/${resource}${name ? `/${encodeURIComponent(name)}` : ""}`;
  }

  corePath(resource, name = "", query = "") {
    return `/api/v1/namespaces/${encodeURIComponent(this.namespace)}/${resource}${name ? `/${encodeURIComponent(name)}` : ""}${query}`;
  }

  async createSession({ id, task, actor, repositoryName, repositoryUrl, resume, template }) {
    const secret = sessionSecret(id, this.namespace, task, actor);
    const job = renderSessionTemplate(template, {
      SESSION_ID: id,
      SESSION_SECRET_NAME: secret.metadata.name,
      REPOSITORY_NAME: repositoryName,
      REPOSITORY_URL: repositoryUrl,
      CALLER_SUBJECT: actor,
      RESUME: resume ? "1" : "0",
    });
    await apiRequest(this.connection, "POST", this.corePath("secrets"), secret);
    try {
      return await apiRequest(this.connection, "POST", this.path("jobs"), job);
    } catch (error) {
      await this.deleteSecret(secret.metadata.name).catch(() => {});
      throw error;
    }
  }

  async getJob(id) {
    try { return await apiRequest(this.connection, "GET", this.path("jobs", `multiagent-session-${id}`)); }
    catch (error) { if (error.statusCode === 404) return null; throw error; }
  }

  async getPod(id) {
    const selector = encodeURIComponent(`multiagent.movement.io/session=${id}`);
    const result = await apiRequest(this.connection, "GET", this.corePath("pods", "", `?labelSelector=${selector}`));
    return (result?.items || []).find((pod) => pod.status?.podIP) || result?.items?.[0] || null;
  }

  async deleteSession(id) {
    const body = { apiVersion: "v1", kind: "DeleteOptions", propagationPolicy: "Foreground" };
    await apiRequest(this.connection, "DELETE", this.path("jobs", `multiagent-session-${id}`), body).catch((error) => {
      if (error.statusCode !== 404) throw error;
    });
    await this.deleteSecret(`multiagent-session-${id}`).catch((error) => {
      if (error.statusCode !== 404) throw error;
    });
  }

  async deleteSecret(name) {
    return apiRequest(this.connection, "DELETE", this.corePath("secrets", name), { apiVersion: "v1", kind: "DeleteOptions" });
  }
}
