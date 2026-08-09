import { randomUUID } from "node:crypto";

import type { ElementHandle, Page } from "playwright-core";

const SNAPSHOT_SELECTOR =
  "h1,h2,h3,h4,h5,h6,p,li,a,button,input,textarea,select,[role],[tabindex]";
// ponytail: hard cap bounds hostile DOM scans; add windowed scanning if >1000 useful nodes matters.
const MAX_ELEMENTS = 1000;

export interface SnapshotElement {
  ref: string;
  role: string;
  name: string;
  state: Record<string, string | boolean>;
}

export interface BrowserSnapshot {
  generation: string;
  url: string;
  title: string;
  elements: SnapshotElement[];
  truncated: boolean;
  next_cursor?: number;
}

export interface SnapshotOptions {
  maxChars: number;
  cursor?: number;
}

export interface ElementMetadata {
  role: string;
  name: string;
  state: Record<string, string | boolean>;
}

interface ScannedElement {
  metadata: ElementMetadata;
  handle?: ElementHandle;
}

interface ScanResult {
  url: string;
  title: string;
  fingerprint: string;
  capped: boolean;
  elements: ScannedElement[];
}

interface SnapshotState {
  generation: string;
  fingerprint: string;
  url: string;
  title: string;
  elements: SnapshotElement[];
  handles: Map<string, ElementHandle>;
  capped: boolean;
}

const states = new WeakMap<Page, SnapshotState>();

export class BrowserSnapshotError extends Error {
  public constructor(
    public readonly code: string,
    message: string,
  ) {
    super(message);
    this.name = "BrowserSnapshotError";
  }
}

export async function takeSnapshot(
  page: Page,
  options: SnapshotOptions,
): Promise<BrowserSnapshot> {
  const cursor = options.cursor ?? 0;
  if (!Number.isInteger(cursor) || cursor < 0 || options.maxChars < 1000) {
    throw new BrowserSnapshotError("browser_snapshot_options", "Snapshot options are invalid");
  }
  const scan = await scanPage(page, true);
  let state = states.get(page);
  if (
    state === undefined ||
    state.fingerprint !== scan.fingerprint ||
    !(await allConnected(state.handles.values()))
  ) {
    if (state !== undefined) await disposeHandles(state.handles.values());
    state = createState(scan);
    states.set(page, state);
  } else {
    await disposeHandles(scan.elements.flatMap((element) => element.handle ?? []));
  }
  const selected: SnapshotElement[] = [];
  let next = cursor;
  while (next < state.elements.length) {
    const candidate = [...selected, state.elements[next] as SnapshotElement];
    const preview = buildSnapshot(state, candidate, next + 1);
    if (JSON.stringify(preview).length > options.maxChars) break;
    selected.push(state.elements[next] as SnapshotElement);
    next += 1;
  }
  if (selected.length === 0 && next < state.elements.length) {
    throw new BrowserSnapshotError("browser_snapshot_budget", "Snapshot budget is too small");
  }
  return buildSnapshot(state, selected, next);
}

export async function resolveRef(
  page: Page,
  generation: string,
  ref: string,
): Promise<ElementHandle> {
  const state = states.get(page);
  if (state === undefined || state.generation !== generation) throw staleRef();
  const current = await scanPage(page, false);
  const handle = state.handles.get(ref);
  if (
    handle === undefined ||
    current.fingerprint !== state.fingerprint ||
    !(await isConnected(handle))
  ) {
    throw staleRef();
  }
  return handle;
}

function createState(scan: ScanResult): SnapshotState {
  const handles = new Map<string, ElementHandle>();
  const elements = scan.elements.map((element, index) => {
    const ref = `@e${index + 1}`;
    if (element.handle !== undefined) handles.set(ref, element.handle);
    return { ref, ...element.metadata };
  });
  return {
    generation: randomUUID(),
    fingerprint: scan.fingerprint,
    url: scan.url,
    title: scan.title,
    elements,
    handles,
    capped: scan.capped,
  };
}

function buildSnapshot(
  state: SnapshotState,
  elements: SnapshotElement[],
  next: number,
): BrowserSnapshot {
  const hasNext = next < state.elements.length;
  const truncated = hasNext || state.capped;
  const snapshot: BrowserSnapshot = {
    generation: state.generation,
    url: state.url,
    title: state.title,
    elements,
    truncated,
  };
  if (hasNext) snapshot.next_cursor = next;
  return snapshot;
}

async function scanPage(page: Page, includeHandles: boolean): Promise<ScanResult> {
  const locator = page.locator(SNAPSHOT_SELECTOR);
  const total = await locator.count();
  const elements: ScannedElement[] = [];
  for (let index = 0; index < Math.min(total, MAX_ELEMENTS); index += 1) {
    const item = locator.nth(index);
    if (!(await item.isVisible())) continue;
    const metadata = await item.evaluate(readElementMetadata);
    const handle = includeHandles ? ((await item.elementHandle()) ?? undefined) : undefined;
    elements.push(handle === undefined ? { metadata } : { metadata, handle });
  }
  const url = page.url();
  const title = await page.title();
  return {
    url,
    title,
    fingerprint: JSON.stringify({ url, title, elements: elements.map((item) => item.metadata) }),
    capped: total > MAX_ELEMENTS,
    elements,
  };
}

export function readElementMetadata(element: Element): ElementMetadata {
  const tag = element.tagName.toLowerCase();
  const input = element instanceof HTMLInputElement ? element : undefined;
  const explicitRole = element.getAttribute("role");
  const roles: Record<string, string> = {
    a: "link",
    button: "button",
    h1: "heading",
    h2: "heading",
    h3: "heading",
    h4: "heading",
    h5: "heading",
    h6: "heading",
    input: input?.type === "checkbox" || input?.type === "radio" ? input.type : "textbox",
    li: "listitem",
    p: "paragraph",
    select: "combobox",
    textarea: "textbox",
  };
  const labels = input?.labels === null ? "" : [...(input?.labels ?? [])]
    .map((label) => label.textContent ?? "")
    .join(" ");
  const labelledBy = (element.getAttribute("aria-labelledby") ?? "")
    .split(/\s+/)
    .filter(Boolean)
    .map((id) => document.getElementById(id)?.textContent ?? "")
    .join(" ");
  const name = (
    element.getAttribute("aria-label") ||
      labelledBy ||
      labels ||
      element.getAttribute("placeholder") ||
      element.textContent ||
      ""
  )
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, 500);
  const state: Record<string, string | boolean> = {};
  if ("disabled" in element) state.disabled = Boolean((element as HTMLButtonElement).disabled);
  if (input?.type === "checkbox" || input?.type === "radio") state.checked = input.checked;
  if (input !== undefined) {
    state.input_kind = ["current-password", "new-password", "one-time-code"].includes(
      input.autocomplete,
    )
      ? input.autocomplete
      : input.type || "text";
  }
  const expanded = element.getAttribute("aria-expanded");
  if (expanded !== null) state.expanded = expanded === "true";
  return { role: explicitRole || roles[tag] || "generic", name, state };
}

async function isConnected(handle: ElementHandle): Promise<boolean> {
  return handle.evaluate((element) => element.isConnected).catch(() => false);
}

async function allConnected(handles: Iterable<ElementHandle>): Promise<boolean> {
  for (const handle of handles) if (!(await isConnected(handle))) return false;
  return true;
}

async function disposeHandles(handles: Iterable<ElementHandle>): Promise<void> {
  await Promise.all([...handles].map((handle) => handle.dispose().catch(() => undefined)));
}

function staleRef(): BrowserSnapshotError {
  return new BrowserSnapshotError("browser_stale_ref", "Browser ref is stale");
}
