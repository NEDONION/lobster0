import { constants } from "node:fs";
import { chmod, lstat, mkdir, open, unlink } from "node:fs/promises";
import { join } from "node:path";

export const PROFILE_LOCK_NAME = ".lobster0-browser.lock";

export class BrowserLifecycleError extends Error {
  public constructor(
    public readonly code: string,
    message: string,
  ) {
    super(message);
    this.name = "BrowserLifecycleError";
  }
}

export class ProfileLock {
  readonly path: string;
  #identity: { dev: bigint; ino: bigint } | undefined;

  public constructor(private readonly profileRoot: string) {
    this.path = join(profileRoot, PROFILE_LOCK_NAME);
  }

  public async acquire(): Promise<void> {
    await ensurePrivateDirectory(this.profileRoot);
    try {
      const handle = await open(
        this.path,
        constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL,
        0o600,
      );
      try {
        const stat = await handle.stat({ bigint: true });
        await handle.writeFile(`${JSON.stringify({ pid: process.pid, started_at: Date.now() })}\n`);
        this.#identity = { dev: stat.dev, ino: stat.ino };
      } finally {
        await handle.close();
      }
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === "EEXIST") {
        throw new BrowserLifecycleError("browser_profile_locked", "Browser profile is locked");
      }
      throw error;
    }
  }

  public async release(): Promise<void> {
    const identity = this.#identity;
    this.#identity = undefined;
    if (identity === undefined) return;
    try {
      const stat = await lstat(this.path, { bigint: true });
      if (stat.dev === identity.dev && stat.ino === identity.ino) await unlink(this.path);
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
    }
  }
}

async function ensurePrivateDirectory(path: string): Promise<void> {
  try {
    const stat = await lstat(path);
    if (stat.isSymbolicLink() || !stat.isDirectory()) {
      throw new BrowserLifecycleError("browser_profile_unsafe", "Browser profile is unsafe");
    }
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
    await mkdir(path, { recursive: true, mode: 0o700 });
  }
  await chmod(path, 0o700);
}
