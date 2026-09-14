// Node leaves exitCode null when a child exits because of a signal.
export function launcherActive(child) {
  return Boolean(child && child.exitCode === null && child.signalCode === null);
}
