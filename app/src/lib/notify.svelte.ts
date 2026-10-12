// Notifications: one stack at the top right of the window, over the page
// instead of inside it. The Server page used to show these as banners around
// the power button: each pushed the page about as it came and went, and a
// closed one came back whenever its text changed (free VRAM, a download's
// percentage) or the page was opened again.
//
// Two kinds. A *condition* is a state that holds for now (a member is missing
// the model, GPU memory is almost full): it has one id, its text is updated in
// place, it has no close button and goes away when the state does. An *event*
// is something that happened (an action failed): it can be closed, and
// anything but an error closes itself after a while.

export type NoteKind = "error" | "warn" | "info";

export interface NoteAction {
  label: string;
  run: () => void;
  busy?: boolean;
}

export interface NoteBody {
  kind: NoteKind;
  text: string;
  action?: NoteAction;
  /** Present: the user may close it (and this runs). Absent: it clears itself. */
  onclose?: () => void;
}

export interface Note extends NoteBody {
  id: string;
}

export const notes = $state<{ list: Note[] }>({ list: [] });

const timers = new Map<string, ReturnType<typeof setTimeout>>();
const EVENT_MS = 9000;
let serial = 0;

/** Shows, updates in place, or (with null) removes the note with this id. */
export function condition(id: string, body: NoteBody | null) {
  const at = notes.list.findIndex((n) => n.id === id);
  if (!body) {
    if (at >= 0) notes.list.splice(at, 1);
    return;
  }
  if (at >= 0) Object.assign(notes.list[at], { action: undefined, onclose: undefined }, body);
  else notes.list.push({ id, ...body });
}

/** Something that happened. Errors stay until closed; the rest close themselves. */
export function notify(kind: NoteKind, text: string, action?: NoteAction): string {
  const id = `event-${++serial}`;
  notes.list.push({ id, kind, text, action, onclose: () => {} });
  if (kind !== "error") timers.set(id, setTimeout(() => dismiss(id), EVENT_MS));
  return id;
}

export function dismiss(id: string) {
  const timer = timers.get(id);
  if (timer) clearTimeout(timer);
  timers.delete(id);
  const at = notes.list.findIndex((n) => n.id === id);
  if (at < 0) return;
  const [note] = notes.list.splice(at, 1);
  note.onclose?.();
}
