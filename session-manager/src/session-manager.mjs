import { generateThreadId } from "./thread-model.mjs";
import { renderThreadTask } from "./thread-context.mjs";
import path from "node:path";

function scopedTranscript(sessionId, transcript) {
  if (!transcript || typeof transcript !== "object") return null;
  const traceReferences = Array.isArray(transcript.traceReferences)
    ? transcript.traceReferences.map((reference) => {
      const normalized = path.posix.normalize(path.posix.join("logs", String(reference)));
      if (path.posix.isAbsolute(normalized) || normalized === ".." || normalized.startsWith("../")) return null;
      return `trace://session/${sessionId}/${normalized}`;
    }).filter(Boolean)
    : [];
  return { ...transcript, traceReferences };
}

function publicResultEvent(sessionId, report) {
  return {
    type: report.responseType || "assistant_message",
    payload: {
      text: String(report.message || "").trim() || String(report.report || "").trim(),
      transcript: scopedTranscript(sessionId, report.transcript),
    },
  };
}

function interruptedResultEvent(sessionId, report, fallback) {
  return {
    type: "session_interrupted",
    payload: {
      text: String(report?.message || "").trim() || String(fallback || "").trim(),
      transcript: scopedTranscript(sessionId, report?.transcript),
    },
  };
}

export class SessionManager {
  constructor({
    threadStore,
    newThreadId = () => generateThreadId(),
    newSessionId,
    startExecution,
    deliverFollowup,
    reconcileExecution = async () => {},
    reconcileThreadExecutions = async () => {},
    markExecutionProjected = async () => {},
  }) {
    if (!threadStore) throw new Error("SessionManager requires a thread store");
    if (typeof newSessionId !== "function") throw new Error("SessionManager requires a session ID factory");
    if (typeof startExecution !== "function") throw new Error("SessionManager requires an execution launcher");
    if (typeof deliverFollowup !== "function") throw new Error("SessionManager requires a follow-up delivery adapter");
    this.threadStore = threadStore;
    this.newThreadId = newThreadId;
    this.newSessionId = newSessionId;
    this.startExecutionAdapter = startExecution;
    this.deliverFollowupAdapter = deliverFollowup;
    this.reconcileExecutionAdapter = reconcileExecution;
    this.reconcileThreadExecutionsAdapter = reconcileThreadExecutions;
    this.markExecutionProjectedAdapter = markExecutionProjected;
  }

  createThread({ ownerSubject, repository, title = "", source = null, id = this.newThreadId() }) {
    return this.threadStore.createThread({ id, ownerSubject, repository, title, source });
  }

  listThreads(actor) {
    return this.threadStore.listThreadsForActor(actor);
  }

  getThread(threadId, actor) {
    return this.threadStore.getThreadForActor(threadId, actor);
  }

  readEvents({ threadId, actor, afterSequence = 0, limit = 200 }) {
    return this.threadStore.readEventsAfter({ threadId, actor, afterSequence, limit });
  }

  async listSessions({ threadId, actor }) {
    await this.threadStore.getThreadForActor(threadId, actor);
    await this.reconcileThreadExecutionsAdapter(threadId);
    return this.threadStore.listSessionsForActor({ threadId, actor });
  }

  listReviews({ actor, status = "pending" }) {
    return this.threadStore.listReviewsForActor({ actor, status });
  }

  async appendMessage({ threadId, actor, messageId, text }) {
    let thread = await this.threadStore.getThreadForActor(threadId, actor);
    if (thread.activeSessionId) {
      await this.reconcileExecutionAdapter(thread.activeSessionId);
      thread = await this.threadStore.getThreadForActor(thread.id, actor);
    }
    const routed = await this.threadStore.appendUserMessageAndRoute({
      threadId: thread.id,
      actor,
      messageId,
      text,
      newSessionId: this.newSessionId(thread.id),
    });
    if (routed.createdSession && routed.session.leaseGeneration !== null) {
      await this.startExecution(thread, routed.session);
    }
    const delivery = routed.session.leaseGeneration === null
      ? { mode: "queued-context" }
      : routed.createdSession
        ? { mode: "initial-context" }
        : await this.deliverFollowup(thread, routed);
    return { ...routed, delivery };
  }

  async deliverFollowup(thread, routed) {
    if (routed.session.inboxAckSequence >= routed.event.sequence) return { mode: "already-delivered" };
    const delivered = await this.deliverFollowupAdapter(thread, routed);
    await this.threadStore.acknowledgeInbox({
      threadId: thread.id,
      sessionId: routed.session.id,
      generation: routed.session.leaseGeneration,
      throughSequence: routed.event.sequence,
    });
    return delivered;
  }

  async createExternalThread({
    ownerSubject,
    repository,
    title,
    sourceActor,
    source,
    eventId,
    text,
    id = this.newThreadId(),
  }) {
    const routed = await this.threadStore.createExternalThreadAndRoute({
      id,
      ownerSubject,
      repository,
      title,
      sourceActor,
      source,
      eventId,
      text,
      newSessionId: this.newSessionId(id),
    });
    if (!routed.duplicate) await this.startExecution(routed.thread, routed.session);
    return routed;
  }

  async decideReview({ reviewId, actor, decision, decisionId, messageId }) {
    const routed = await this.threadStore.decideReviewAndRoute({
      reviewId,
      actor,
      decision,
      decisionId,
      messageId,
      newSessionId: decision === "approve" ? this.newSessionId(`review-${reviewId}`) : undefined,
    });
    if (routed.session) await this.startExecution(routed.thread, routed.session);
    return routed;
  }

  async startExecution(thread, session) {
    const envelope = await this.threadStore.contextEnvelope({
      threadId: thread.id,
      actor: thread.ownerSubject,
      sessionId: session.id,
    });
    const task = renderThreadTask(envelope, session.triggerMessageId);
    try {
      await this.startExecutionAdapter({ thread, session, task });
      const running = await this.threadStore.markSessionRunning({
        threadId: thread.id,
        sessionId: session.id,
        generation: session.leaseGeneration,
      });
      const sessions = await this.threadStore.listSessionsForActor({ threadId: thread.id, actor: thread.ownerSubject });
      const current = sessions.find((candidate) => candidate.id === session.id);
      if (current && current.inboxAckSequence < session.inboxHeadSequence) {
        await this.threadStore.acknowledgeInbox({
          threadId: thread.id,
          sessionId: session.id,
          generation: session.leaseGeneration,
          throughSequence: session.inboxHeadSequence,
        });
      }
      return running;
    } catch (error) {
      await this.threadStore.finalizeSession({
        threadId: thread.id,
        sessionId: session.id,
        generation: session.leaseGeneration,
        status: "interrupted",
      });
      throw error;
    }
  }

  async projectExecution({ record, status, report }) {
    if (!record?.threadId || record.threadProjectedAt) return { projected: false };
    const terminalOutcome = report?.terminalOutcome
      || (status === "failed" || status === "paused" ? "failed" : null);
    if (!terminalOutcome) return { projected: false };
    if (terminalOutcome === "succeeded" || terminalOutcome === "review_requested") {
      if (!report?.report) return { projected: false };
      const sessions = await this.threadStore.listSessionsForActor({
        threadId: record.threadId,
        actor: record.createdBy,
      });
      const session = sessions.find((candidate) => candidate.id === record.id);
      if (!session || session.inboxAckSequence !== session.inboxHeadSequence) return { projected: false };
      const publicEvent = publicResultEvent(record.id, report);
      await this.threadStore.appendFencedSessionEvent({
        threadId: record.threadId,
        sessionId: record.id,
        generation: record.leaseGeneration,
        eventId: `final-${record.id}`,
        ...publicEvent,
      });
      await this.threadStore.markSessionFinishing({
        threadId: record.threadId,
        sessionId: record.id,
        generation: record.leaseGeneration,
      });
      const reviewRequired = terminalOutcome === "review_requested" && publicEvent.type === "question";
      const finalized = reviewRequired
        ? await this.threadStore.finalizeSessionWithReview({
          threadId: record.threadId,
          sessionId: record.id,
          generation: record.leaseGeneration,
          reviewId: `review-${record.id}`,
          question: publicEvent.payload.text,
          sourceEventId: `final-${record.id}`,
        })
        : await this.threadStore.finalizeSession({
          threadId: record.threadId,
          sessionId: record.id,
          generation: record.leaseGeneration,
        });
      await this.markExecutionProjectedAdapter(record, terminalOutcome);
      if (finalized.activatedSession) {
        const thread = await this.threadStore.getThreadForActor(record.threadId, record.createdBy);
        await this.startExecution(thread, finalized.activatedSession);
      }
      return { projected: true, terminalOutcome };
    }

    const publicEvent = interruptedResultEvent(
      record.id,
      report,
      status === "paused" ? "Execution session paused" : "Execution session failed",
    );
    await this.threadStore.appendFencedSessionEvent({
      threadId: record.threadId,
      sessionId: record.id,
      generation: record.leaseGeneration,
      eventId: `interrupted-${record.id}`,
      ...publicEvent,
    });
    const finalized = await this.threadStore.finalizeSession({
      threadId: record.threadId,
      sessionId: record.id,
      generation: record.leaseGeneration,
      status: "interrupted",
    });
    await this.markExecutionProjectedAdapter(record, terminalOutcome);
    if (finalized.activatedSession) {
      const thread = await this.threadStore.getThreadForActor(record.threadId, record.createdBy);
      await this.startExecution(thread, finalized.activatedSession);
    }
    return { projected: true, terminalOutcome };
  }
}
