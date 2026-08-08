import type { Terminal } from "@earendil-works/pi-tui";

/** In-memory pi-tui Terminal used for deterministic viewport and input tests. */
export class MemoryTerminal implements Terminal {
  public columns: number;
  public rows: number;
  public readonly kittyProtocolActive = false;
  public readonly writes: string[] = [];
  public title = "";
  private input?: (data: string) => void;
  private resize?: () => void;

  public constructor(columns = 80, rows = 24) {
    this.columns = columns;
    this.rows = rows;
  }

  public start(onInput: (data: string) => void, onResize: () => void): void {
    this.input = onInput;
    this.resize = onResize;
  }

  public stop(): void {
    this.input = undefined;
    this.resize = undefined;
  }

  public async drainInput(): Promise<void> {}

  public write(data: string): void {
    this.writes.push(data);
  }

  public moveBy(): void {}
  public hideCursor(): void {}
  public showCursor(): void {}
  public clearLine(): void {}
  public clearFromCursor(): void {}
  public clearScreen(): void {}
  public setProgress(): void {}

  public setTitle(title: string): void {
    this.title = title;
  }

  public send(data: string): void {
    this.input?.(data);
  }

  public resizeTo(columns: number, rows: number): void {
    this.columns = columns;
    this.rows = rows;
    this.resize?.();
  }
}
