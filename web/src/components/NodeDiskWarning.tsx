import { fmtBytes } from "../lib/format";

/** below this much free space on the disk of the PC the node is in danger: 3 ГБ, the unit of fmtBytes. Not the installer's
 * 14 ГБ: that is checked before the node takes its ~12 ГБ, a healthy PC has less than that left */
export const LOW_DISK_BYTES = 3 * 1024 ** 3;

/**
 * The last `disk_free_bytes` the node reported (node-metrics.sh: the disk of the PC, or the models volume where the node
 * cannot see it). No sample: nothing is said, the monitoring page shows what is missing.
 */
export function NodeDiskWarning({ freeBytes }: { freeBytes: number | null | undefined }) {
  if (typeof freeBytes !== "number" || freeBytes >= LOW_DISK_BYTES) return null;
  return (
    <span className="tag warn small" role="alert">
      Мало места на диске ПК: свободно {fmtBytes(freeBytes)}, нода остановится при заполнении диска
    </span>
  );
}
