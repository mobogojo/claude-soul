#!/usr/bin/env node

// Lightweight indexer for Stop hook — only indexes new journals and lessons.
// Embeds all pending entries in parallel so the hook completes quickly even
// when a large backlog has accumulated. Exits 0 on any error (hooks must be resilient).

import fs from "node:fs";
import path from "node:path";
import os from "node:os";
import { getDb, generateId, closeDb } from "../memory/db.js";
import { embed, embeddingToBuffer } from "../memory/embeddings.js";

const HOME = os.homedir();
const debug = (msg: string) => process.stderr.write(`[index-new] ${msg}\n`);

async function indexNewJournals(): Promise<number> {
  const dir = path.join(HOME, ".soul", "journals");
  if (!fs.existsSync(dir)) return 0;

  const db = getDb();
  const files = fs.readdirSync(dir).filter((f) => f.endsWith(".md"));

  type PendingEntry = { entryContent: string; project: string | null; created: number };
  const pending: PendingEntry[] = [];

  for (const file of files) {
    const filePath = path.join(dir, file);
    const content = fs.readFileSync(filePath, "utf-8").trim();
    if (!content) continue;

    const entries = content.split(/^## /m).filter((e) => e.trim().length > 20);

    for (const entry of entries) {
      const entryContent = `## ${entry}`.trim();

      const existing = db
        .prepare(`SELECT id FROM journal_entries WHERE content LIKE ? LIMIT 1`)
        .get(`${entryContent.slice(0, 80)}%`) as { id: string } | undefined;
      if (existing) continue;

      const headerMatch = entry.match(/^\d{2}:\d{2}\s*—\s*(\S+)/);
      const project = headerMatch?.[1] ?? null;

      const dateMatch = file.match(/(\d{4}-\d{2}-\d{2})/);
      const created = dateMatch ? new Date(dateMatch[1]).getTime() : Date.now();

      pending.push({ entryContent, project, created });
    }
  }

  if (pending.length === 0) return 0;

  debug(`embedding ${pending.length} journal entries in parallel...`);
  const embeddings = await Promise.all(
    pending.map((p) => embed(p.entryContent.slice(0, 2000))),
  );

  for (let i = 0; i < pending.length; i++) {
    const { entryContent, project, created } = pending[i];
    db.prepare(
      `INSERT INTO journal_entries (id, session_id, project, content, created_at, embedding)
       VALUES (?, ?, ?, ?, ?, ?)`,
    ).run(
      generateId("jrn"),
      null,
      project,
      entryContent,
      created,
      embeddings[i] ? embeddingToBuffer(embeddings[i]!) : null,
    );
  }

  return pending.length;
}

async function indexNewLessons(): Promise<number> {
  const lessonsPath = path.join(HOME, ".soul", "data", "lessons.json");
  if (!fs.existsSync(lessonsPath)) return 0;

  const db = getDb();
  const data = JSON.parse(fs.readFileSync(lessonsPath, "utf-8")) as Array<{
    id: string;
    lesson: string;
    context: string;
    confidence: number;
  }>;

  type PendingLesson = { id: string; content: string };
  const pending: PendingLesson[] = [];

  for (const lesson of data) {
    const content = `${lesson.lesson}\nContext: ${lesson.context}`;

    const existing = db
      .prepare(`SELECT id FROM memories WHERE source_file = ?`)
      .get(`lesson:${lesson.id}`) as { id: string } | undefined;
    if (existing) continue;

    pending.push({ id: lesson.id, content });
  }

  if (pending.length === 0) return 0;

  debug(`embedding ${pending.length} lessons in parallel...`);
  const embeddings = await Promise.all(pending.map((p) => embed(p.content)));

  const now = Date.now();
  for (let i = 0; i < pending.length; i++) {
    const { id, content } = pending[i];
    db.prepare(
      `INSERT INTO memories (id, content, category, project, source_file, created_at, updated_at, accessed_at, embedding)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)`,
    ).run(
      generateId("les"),
      content,
      "lesson",
      null,
      `lesson:${id}`,
      now,
      now,
      now,
      embeddings[i] ? embeddingToBuffer(embeddings[i]!) : null,
    );
  }

  return pending.length;
}

async function main() {
  const start = Date.now();
  debug("starting");

  const journals = await indexNewJournals();
  const lessons = await indexNewLessons();

  const elapsed = ((Date.now() - start) / 1000).toFixed(1);
  debug(`done in ${elapsed}s — ${journals} journal(s), ${lessons} lesson(s)`);

  if (journals + lessons > 0) {
    const logPath = path.join(HOME, ".soul", "data", "index-log.txt");
    const msg = `[${new Date().toISOString()}] Auto-indexed: ${journals} journal(s), ${lessons} lesson(s) in ${elapsed}s\n`;
    fs.appendFileSync(logPath, msg);
  }
}

main()
  .catch(() => {})
  .finally(() => {
    closeDb();
    process.exit(0);
  });
