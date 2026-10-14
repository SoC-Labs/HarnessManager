// Every user-visible string of the single-user mode UI (the board's own lease), in one place so
// the guide can quote it. Functions take the few values a sentence needs. Strings freeze
// Saturday 12:00 of the release week: change them only with the guide lead.

export const LEASE_WORDS = {
  // The two servers a lease can live on (week.js serverWord).
  serverHub: "the hub",
  serverBoard: "the board",

  // Board > Access: the card.
  cardTitle: "Who can use this board",
  cardSub: "Whether people share this board or one person at a time drives it.",
  segLabel: "Who can use this board",
  segShared: "Shared",
  segOne: "One person at a time",
  help: "One person at a time: whoever holds the lease can program, reset and debug. Everyone else can watch and ask for it. Someone at the board can always switch it off on the touchscreen.",
  courtesy: "Courtesy lock: it keeps people out of each other's way and keeps out anyone without a key, but anyone whose key is on the board can still get round it.",
  reading: "Reading the board's mode…",
  stateShared: "Shared: anyone with a key can drive it, at the same time as others.",
  stateOne: "One person at a time.",
  stateOneBy: (who) => `One person at a time, switched on by ${who}.`,

  // Why the control is off.
  unclaimed: "Claim the board first (SSH claim, above): the lease needs to know whose key is whose.",
  behindHub: "Behind a hub, the hub's lease already does this.",
  noFeature: "This board's release can't do single-user mode.",
  notOwner: "Only the person who claimed the board can switch this on.",
  notOwnerOff: "Only the person who claimed the board, or whoever holds the lease, can switch this off.",
  localHold: (min) => `Switched off at the board's touchscreen; it can be switched on again in ${min} min.`,

  // The confirm dialogs.
  onTitle: (board) => `Switch ${board} to one person at a time?`,
  onBody: (ttl) => `You take the lease now (Yours · ${ttl}, renewed while the board is open here). Others with a key can still watch it, see that you have it, and ask for it. Someone at the board can always switch it off on the touchscreen.`,
  onButton: "Switch and take the lease",
  offTitle: (board) => `Switch ${board} back to shared?`,
  offBody: "Anyone with a key can drive it again, at the same time as others.",
  offButton: "Switch to shared",
  offForceTitle: (board) => `Switch ${board} back to shared?`,
  offForceBody: (holder, board) => `${holder} holds the lease and is using ${board}. Switching to shared ends their lease now; anything they are running is interrupted. Type the board's name to go ahead.`,
  offForceLabel: "Board name",
  offForceButton: "Switch to shared",
  cancel: "Cancel",
  working: "Switching…",
  doneOn: (board) => `${board} is now one person at a time, and you hold the lease.`,
  doneOff: (board) => `${board} is shared again.`,
  failed: (msg) => `Could not change the mode: ${msg}`,

  // The overview card (Access) and the lease card.
  ovShared: "Shared",
  ovSharedSub: "a desk board on this network",
  ovOneLink: "One person at a time…",
  ovSourceBoard: "Source: the board's own lease (single-user mode)",

  // Keep is binding on a board: heartbeats cannot extend past it.
  keepingFor: (min, who) => `Keeping it for ${min} min (asked by ${who})`,
  requestDefault: "may I use the board?",
  lapsed: "You were next but did not take it in time; ask again.",

  // The banner when someone switched it off at the touchscreen.
  takenAtBoard: (board, at) => `${board}'s lease was ended at the board${at ? ` at ${at}` : ""}: someone switched off single-user mode on the touchscreen. The board is shared again.`,
  takenAtBoardSub: "You can keep using the board, now at the same time as others.",
  modeEvent: (board, on) => (on ? `${board} is now one person at a time.` : `${board} is shared again.`),
};
