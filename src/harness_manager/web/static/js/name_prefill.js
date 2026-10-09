// What the bring-up wizard proposed for a new board (name, MAC, IP), kept until the board is
// claimed and its "Name this board" dialog is opened: the wizard's last step leads to the claim
// first (naming needs it), so the proposal waits here for the dialog the claim leads to.

const PENDING = new Map();

export function setNamePrefill(bid, value) {
  if (value) PENDING.set(bid, value); else PENDING.delete(bid);
}

export function hasNamePrefill(bid) {
  return PENDING.has(bid);
}

// The dialog's arguments for this board: {prefill, impl} when the wizard proposed one, else {}.
export function namePrefillArgs(bid) {
  return PENDING.get(bid) || {};
}

export function clearNamePrefill(bid) {
  PENDING.delete(bid);
}
