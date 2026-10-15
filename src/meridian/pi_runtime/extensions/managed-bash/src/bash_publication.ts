import { writeJsonAtomic } from "../../shared/json_file";
import type { BashRecord, BashRecordsFile } from "../../shared/schemas";

/** What the records file says: the only value `BashPublication` ever writes or adopts. Live
 * execution state (handles, progress, first faults) is not part of it and cannot leak into it. */
export type Published = {
  readonly rows: ReadonlyMap<string, Readonly<BashRecord>>;
  readonly runtimeError?: string;
};

export const NOTHING_PUBLISHED: Published = { rows: new Map() };

/** One metadata change. `stage` derives the whole prospective value; it is written, and only then
 * adopted. A rejected stage or write adopts nothing. */
export type Publication = {
  /** Sees the current value with live evidence already merged in. `null` declines. */
  stage: (base: Published) => Published | null;
  /** Task-fatal policy. Runs synchronously inside this slot, before any successor starts, so no caller
   * supervision (or extra async wrapper around the returned promise) can lose that ordering.
   * It must claim and return: cleanup and repair publication run outside the slot. */
  fatal?: (error: unknown) => void;
  /** Non-metadata effects, run synchronously in the slot right after adoption. */
  adopted?: (next: Published) => void;
};

export function withRow(base: Published, row: Readonly<BashRecord>): Published {
  return { ...base, rows: new Map(base.rows).set(row.bash_id, row) };
}

export function withoutRows(base: Published, ids: Iterable<string>): Published {
  const rows = new Map(base.rows);
  for (const id of ids) rows.delete(id);
  return { ...base, rows };
}

export function withRuntimeError(base: Published, runtimeError: string | undefined): Published {
  return { rows: base.rows, ...(runtimeError === undefined ? {} : { runtimeError }) };
}

/** Stage one row's change; `change` returns the fields to set, or `null` to decline. */
export function changeRow(id: string, change: (row: Readonly<BashRecord>) => Partial<BashRecord> | null) {
  return (base: Published): Published | null => {
    const row = base.rows.get(id);
    const patch = row && change(row);
    return row && patch ? withRow(base, { ...row, ...patch }) : null;
  };
}

/** Serialized publication of the records file. Slots run strictly one at a time; each stages
 * against the value adopted by its predecessor, so a transition can never be written early by
 * another slot and a failed write never leaves its change behind. */
export class BashPublication {
  private published: Published = NOTHING_PUBLISHED;
  private tail: Promise<void> = Promise.resolve();

  constructor(private readonly options: {
    path: string;
    runId: string;
    /** Merges live evidence (progress, failure overlays) into the value a slot stages against. */
    evidence: (published: Published) => Published;
  }) {}

  get current(): Published { return this.published; }

  /** Resolves `false` when staging declined. */
  publish(publication: Publication): Promise<boolean> {
    const slot = this.tail.then(async () => {
      let next: Published | null;
      try {
        next = publication.stage(this.options.evidence(this.published));
        if (!next) return false;
        await this.write(next);
      } catch (error) {
        publication.fatal?.(error);
        throw error;
      }
      this.published = next;
      publication.adopted?.(next);
      return true;
    });
    this.tail = slot.then(() => undefined, () => undefined);
    return slot;
  }

  private async write(next: Published): Promise<void> {
    const file: BashRecordsFile = {
      v: 1,
      spawn_id: this.options.runId,
      updated_at_ms: Date.now(),
      records: Object.fromEntries(next.rows),
      ...(next.runtimeError !== undefined ? { runtime_error: next.runtimeError } : {}),
    };
    await writeJsonAtomic(this.options.path, file);
  }
}
