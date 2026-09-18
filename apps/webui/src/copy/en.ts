// Every sentence the sign-in, shell, Home, Admin → People, Admin → Settings and Models pages
// show (CLAUDE.md §9; ADR-0009). The text is written first in docs/ui/sign-in.md,
// docs/ui/admin-people.md, docs/ui/admin-settings.md and docs/ui/models.md and copied here
// verbatim; change the document, then this file. Sentences the api sends (every non-2xx
// body) are rendered as received and are not repeated here.

import { PRODUCT_NAME } from "../branding";

export interface ThreePart {
  whatHappened: string;
  likelyCause: string;
  whatToDo: string;
}

/** The fallback when the api did not answer at all (network failure or a non-JSON reply). */
export const serverNotAnswering: ThreePart = {
  whatHappened: "The server didn't answer.",
  likelyCause: "The api service is starting or stopped.",
  whatToDo: "Wait a moment and try again; if it repeats, run `slas logs api` on the host.",
};

/** The `slas logs api` sentence most pages use as their third part. */
const LOGS_API = "if it repeats, run `slas logs api` on the host.";

// --- Sign in ---------------------------------------------------------------------------------

export const signIn = {
  fallbackHeading: PRODUCT_NAME,
  lede: "Use the account an administrator created for you. Nothing you type leaves this network.",
  email: "Email",
  password: "Password",
  button: "Sign in",
  buttonWaiting: "Signing in…",
  buttonRetry: "Try again",
  stillChecking: "Still checking… the server is slow to answer.",
  signedOut: "You're signed out.",
  sessionEnded: (lifetime: string) => `Your sign-in ended after ${lifetime}. Sign in again to continue.`,
  serverNotAnswering: {
    whatHappened: "The sign-in server didn't answer.",
    likelyCause: "The api service is starting or stopped.",
    whatToDo: `Wait a moment and press Try again; ${LOGS_API}`,
  } satisfies ThreePart,
  // The api sends these; the in-memory fake sends the same words so the page is tested with them.
  wrongPassword: {
    whatHappened: "That email and password don't match.",
    likelyCause: "A typo, or the password was changed.",
    whatToDo: "Try again, or ask an administrator to reset your password.",
  } satisfies ThreePart,
  switchedOff: {
    whatHappened: "This account is switched off.",
    likelyCause: "An administrator switched it off.",
    whatToDo: "Ask an administrator to switch it back on.",
  } satisfies ThreePart,
  tooManyAttempts: {
    whatHappened: "Too many sign-in attempts in the last 15 minutes.",
    likelyCause: "Several wrong passwords were tried for this account.",
    whatToDo: "Wait and try again.",
  } satisfies ThreePart,
  rateLimiterDown: {
    whatHappened: "Sign-in is paused for a moment.",
    likelyCause: "The service that counts sign-in attempts didn't answer.",
    whatToDo: "Try again in a minute; if it repeats, run `slas logs redis` on the host.",
  } satisfies ThreePart,
  mustChangeFirst: "Choose a new password first.",
};

/** "8 hours", "1 day", "7 days" — the session lifetime as people say it. */
export function lifetimeWords(hours: number): string {
  if (hours % 24 === 0 && hours >= 24) {
    const days = hours / 24;
    return days === 1 ? "1 day" : `${days} days`;
  }
  return hours === 1 ? "1 hour" : `${hours} hours`;
}

// --- Choose a new password -------------------------------------------------------------------

export const choosePassword = {
  heading: "Choose a new password",
  lede: "Your password was made for one use. Pick one only you know.",
  help: "At least 12 characters. Longer beats complicated.",
  newPassword: "New password",
  again: "Type it again",
  // Shown only after a reload, when the password typed at sign-in is no longer in memory
  // (POST /api/v1/me/password needs it). Not in docs/ui/sign-in.md; see the report.
  current: "Current password",
  currentHelp: "The one-time password you signed in with.",
  button: "Save and continue",
  buttonWaiting: "Saving…",
  minLength: 12,
  mismatch: {
    whatHappened: "The two passwords don't match.",
    likelyCause: "A typo in one of them.",
    whatToDo: "Type both again.",
  } satisfies ThreePart,
  tooShort: (n: number): ThreePart => ({
    whatHappened: `The password is too short: it has ${n} characters and needs at least 12.`,
    likelyCause: "",
    whatToDo: "Add a few more words.",
  }),
  sameAsEmail: {
    whatHappened: "The password can't be your email address.",
    likelyCause: "",
    whatToDo: "Choose something only you know.",
  } satisfies ThreePart,
  serverNotAnswering: {
    whatHappened: "The password wasn't saved.",
    likelyCause: "The api service didn't answer.",
    whatToDo: `Press Save and continue again; ${LOGS_API}`,
  } satisfies ThreePart,
};

// --- Shell, Home, guards ---------------------------------------------------------------------

export const shell = {
  signedInAs: (name: string, roleLabel: string) => `Signed in as ${name} · ${roleLabel}`,
  signedInAsPlain: (name: string) => `Signed in as ${name}`,
  signOut: "Sign out",
  checking: "Checking your sign-in…",
  brandLine: "Self-hosted, no cloud",
  airGapped: "Air-gapped mode is on",
  version: (v: string) => `Version ${v}`,
  rail: {
    home: "Home",
    coding: "Coding",
    validation: "Validation",
    factory: "Factory",
    runs: "Runs",
    models: "Models",
    skills: "Skills",
    settings: "Settings",
    admin: "Admin",
  },
  adminTabs: {
    people: "People",
    settings: "Settings",
    gitHosts: "Git hosts",
    stations: "Stations",
  },
};

export const notAllowed = {
  sentence: "This part is for administrators. Ask an administrator if you need something changed here.",
  link: "Go to Home",
};

export const unknownAddress = {
  sentence: "There is nothing at this address.",
  link: "Go to Home",
};

/** Pages that arrive later (docs/ui/home.md): one panel each, never a blank page. */
export const later = {
  runs: {
    heading: "Runs",
    lede: "Every ticket, across the three agents.",
    sentence:
      "Every ticket from the three agents will be listed here once the ticket service is connected (Phase 3). Until then, each agent's page lists its own work.",
  },
  models: {
    heading: "Models",
    lede: "Which model serves each role, and the cross-check voters.",
    sentence:
      "Models are read from Models/models.yaml. This page arrives with the Models service (Phase 3); until then, edit the file on the host and run `slas model fit` before a load.",
  },
  skills: {
    heading: "Skills",
    lede: "Reusable step-by-step recipes. Write one once, then turn it on for any agent.",
    sentence:
      "The skill library and its per-agent switches arrive with Phase 4 (ADR-0013). Skills already imported are offered by the New task, run and job wizards.",
  },
};

/** What a person can do, in sentences built from their capabilities (docs/ui/sign-in.md, Home). */
const CAN_DO: [capability: string, phrase: string][] = [
  ["screen", "operate screens"],
  ["ssh", "run commands on targets"],
  ["git:remote_manage", "work with Git remotes"],
  ["approve:destructive", "approve destructive steps"],
  ["factory:verdict", "decide factory verdicts"],
];

export function homeWelcome(
  displayName: string,
  capabilities: readonly string[],
  options: { agentsPresent: boolean },
): string {
  const later = options.agentsPresent ? "" : " Coding, Validation and Factory arrive in later phases";
  const isAdmin = capabilities.includes("admin:people") || capabilities.includes("admin:settings");
  if (isAdmin) {
    const tail = later === "" ? " People and Settings are under Admin." : `${later}; People and Settings are under Admin.`;
    return `Signed in as ${displayName}.${tail}`;
  }
  const phrases = CAN_DO.filter(([cap]) => capabilities.includes(cap)).map(([, phrase]) => phrase);
  const canDo =
    phrases.length === 0
      ? "You can read tickets, runs and reports."
      : `You can ${phrases.length === 1 ? phrases[0] : `${phrases.slice(0, -1).join(", ")} and ${phrases[phrases.length - 1]}`}.`;
  return `Signed in as ${displayName}. ${canDo}${later === "" ? "" : `${later}.`}`;
}

// --- Roles (mirrors config/rbac-roles.yaml, ADR-0006; the api has no roles route in round 1) --

export interface RoleInfo {
  id: string;
  label: string;
  sentence: string;
}

export const ROLES: readonly RoleInfo[] = [
  {
    id: "administrator",
    label: "Administrator",
    sentence:
      "Manages people, settings, Git hosts, test stations and models, and can do everything an engineer can.",
  },
  {
    id: "engineer",
    label: "Engineer",
    sentence: "Runs coding tasks, validation runs and factory jobs, and approves destructive steps.",
  },
  {
    id: "line_lead",
    label: "Line lead",
    sentence: "An engineer who also decides a factory PASS or FAIL when the voters disagree.",
  },
  { id: "viewer", label: "Viewer", sentence: "Reads tickets, runs and reports." },
];

export const DEFAULT_ROLE = "engineer";

export function roleInfo(id: string): RoleInfo {
  return ROLES.find((role) => role.id === id) ?? { id, label: id, sentence: "" };
}

// --- Admin → People --------------------------------------------------------------------------

export const people = {
  heading: "People",
  lede: "Who can sign in, and what each person may do.",
  addButton: "Add person",
  columns: ["Person", "Role", "Can do", "Last sign-in", "Status"] as const,
  never: "Never",
  status: {
    canSignIn: "Can sign in",
    switchedOff: "Switched off",
    mustChoose: "Must choose a password",
  },
  menu: {
    label: (name: string) => `Actions for ${name}`,
    changeRole: "Change role",
    resetPassword: "Reset password",
    switchOff: "Switch off",
    switchOn: "Switch on",
  },
  loading: "Loading people…",
  onlyYou: "Only you so far. Add the engineers who will use the platform; each gets a one-time password.",
  failedToLoad: {
    whatHappened: "The list of people didn't load.",
    likelyCause: "The api service didn't answer.",
    whatToDo: `Press Try again; ${LOGS_API}`,
  } satisfies ThreePart,
  tryAgain: "Try again",
  add: {
    title: "Add a person",
    name: "Name",
    nameHelp: "Shown on their commits and tickets.",
    email: "Email",
    emailHelp: "They sign in with it. It doesn't have to reach the internet.",
    role: "Role",
    closing: (name: string) => `${name} will get a one-time password and choose their own at first sign-in.`,
    button: (name: string) => (name === "" ? "Add person" : `Add ${name}`),
    buttonWaiting: "Adding…",
    notAdded: (name: string): ThreePart => ({
      whatHappened: `${name} wasn't added.`,
      likelyCause: "The api service didn't answer.",
      whatToDo: `Press Add again; ${LOGS_API}`,
    }),
  },
  result: {
    title: (name: string) => `${name} can sign in now`,
    body: (name: string) =>
      `Give ${name} this one-time password. It works once; they choose their own at first sign-in. You won't see it again.`,
    copy: "Copy",
    copied: "Copied",
    done: "Done",
    passwordLabel: "One-time password",
  },
  changeRole: {
    title: (name: string) => `Change ${name}'s role`,
    closing: (name: string, roleLabel: string) =>
      `${name} becomes a ${roleLabel} on their next request; nothing else changes.`,
    button: "Change role",
    buttonWaiting: "Changing…",
    notChanged: (name: string): ThreePart => ({
      whatHappened: `${name}'s role wasn't changed.`,
      likelyCause: "The api service didn't answer.",
      whatToDo: `Press Change role again; ${LOGS_API}`,
    }),
  },
  resetPassword: {
    title: (name: string) => `Reset ${name}'s password`,
    body: (name: string) =>
      `${name}'s current password stops working, they are signed out everywhere and get a new one-time password.`,
    button: "Reset password",
    buttonWaiting: "Resetting…",
    notReset: (name: string): ThreePart => ({
      whatHappened: `${name}'s password wasn't reset.`,
      likelyCause: "The api service didn't answer.",
      whatToDo: `Press Reset password again; ${LOGS_API}`,
    }),
  },
  switchOff: {
    title: (name: string) => `Switch off ${name}'s account`,
    body: (name: string) =>
      `${name} can't sign in until switched back on, and is signed out everywhere now. Their history stays.`,
    button: "Switch off",
    buttonWaiting: "Switching off…",
    yourself: {
      whatHappened: "You can't switch off your own account.",
      likelyCause: "You're signed in with it.",
      whatToDo: "Ask another administrator.",
    } satisfies ThreePart,
    notSwitched: (name: string): ThreePart => ({
      whatHappened: `${name}'s account wasn't switched off.`,
      likelyCause: "The api service didn't answer.",
      whatToDo: `Press Switch off again; ${LOGS_API}`,
    }),
  },
  switchOn: {
    title: (name: string) => `Switch on ${name}'s account`,
    body: (name: string) => `${name} can sign in again with their current password.`,
    button: "Switch on",
    buttonWaiting: "Switching on…",
    notSwitched: (name: string): ThreePart => ({
      whatHappened: `${name}'s account wasn't switched on.`,
      likelyCause: "The api service didn't answer.",
      whatToDo: `Press Switch on again; ${LOGS_API}`,
    }),
  },
  cancel: "Cancel",
  // The api sends these; the in-memory fake sends the same words.
  duplicateEmail: (email: string): ThreePart => ({
    whatHappened: `Someone already signs in as ${email}.`,
    likelyCause: "The address belongs to an existing person, maybe switched off.",
    whatToDo: "Use another address, or switch the existing account back on.",
  }),
  invalidEmail: {
    whatHappened: "That doesn't look like an email address.",
    likelyCause: "A missing @ or domain.",
    whatToDo: "Check it and try again.",
  } satisfies ThreePart,
  lastAdministratorRole: {
    whatHappened: "You can't change the last administrator's role.",
    likelyCause: "Without an administrator nobody could manage people or settings.",
    whatToDo: "Make someone else an administrator first.",
  } satisfies ThreePart,
  lastAdministratorOff: {
    whatHappened: "You can't switch off the last administrator.",
    likelyCause: "Without an administrator nobody could manage people or settings.",
    whatToDo: "Make someone else an administrator first.",
  } satisfies ThreePart,
};

// --- Admin → Settings ------------------------------------------------------------------------

export const settings = {
  heading: "Settings",
  lede: "Changes apply as soon as you save. Nothing is restarted.",
  loading: "Loading settings…",
  failedToLoad: {
    whatHappened: "The settings didn't load.",
    likelyCause: "The api service didn't answer.",
    whatToDo: `Press Try again; ${LOGS_API}`,
  } satisfies ThreePart,
  tryAgain: "Try again",
  runtimeHeading: "You can change these now",
  installationName: {
    label: "Name shown on the sign-in page",
    help: "Helps people tell installations apart, for example Lab 3.",
    maxLength: 60,
  },
  chineseVariant: {
    label: "Chinese used in reports and SOPs",
    help: "English is always produced alongside.",
    choices: [
      { value: "zh-Hant", label: "Traditional (繁體) — default" },
      { value: "zh-Hans", label: "Simplified (简体)" },
    ] as const,
  },
  sessionLifetime: {
    label: "How long a sign-in lasts",
    help: "Applies to new sign-ins; nobody is signed out.",
    choices: [
      { value: 8, label: "8 hours" },
      { value: 24, label: "1 day" },
      { value: 168, label: "7 days" },
    ] as const,
  },
  save: "Save changes",
  saving: "Saving…",
  saved: (time: string) => `Saved at ${time}. It applies now; nothing restarted.`,
  mirrorFailed: (envPath: string): ThreePart => ({
    whatHappened: "Saved, but the copy in `.env` couldn't be written.",
    likelyCause: "The data root isn't writable by the api service.",
    whatToDo: `The settings apply now; fix permissions on ${envPath} so they survive a reinstall.`,
  }),
  nameTooLong: (n: number): ThreePart => ({
    whatHappened: `The name is too long: it has ${n} characters, the limit is 60.`,
    likelyCause: "",
    whatToDo: "Shorten it.",
  }),
  notSaved: {
    whatHappened: "The settings weren't saved.",
    likelyCause: "The database didn't answer.",
    whatToDo: `Try again in a moment; ${LOGS_API}`,
  } satisfies ThreePart,
  installHeading: "Set at install",
  facts: {
    dataRoot: "Where data is stored",
    port: "Web port",
    tlsNames: "Addresses on the certificate",
    tlsMode: "TLS certificate",
    profile: "Profile",
    version: "Version",
  },
  tlsModeWords: (mode: string) =>
    mode === "self-signed" ? "self-signed by this installation" : mode === "provided" ? "provided at install" : mode,
  footer:
    "To change these, edit `.env` on the host and run `./install.sh` again; only the affected service is restarted.",
};

// --- Models ----------------------------------------------------------------------------------

export const models = {
  heading: "Models",
  lede: "Which model serves each role, the cross-check voters, and adding a model from a link.",
  loading: "Loading models…",
  failedToLoad: {
    whatHappened: "The model registry didn't load.",
    likelyCause: "The api service didn't answer.",
    whatToDo: `Press Try again; ${LOGS_API}`,
  } satisfies ThreePart,
  tryAgain: "Try again",
  modelsHeading: "Models on this installation",
  rolesHeading: "Who serves each role",
  votersHeading: "Cross-check voters",
  noModels: "No models are listed. Fix Models/models.yaml on the host; the format is in services/model-manager/README.md.",
  noRoles: "No role is assigned yet.",
  noVoters: "No voters are configured, so cross-checks run as a single model and are flagged.",
  present: "weights present",
  notPresent: "weights not here yet",
  family: (family: string) => `${family} family`,
  vram: (gib: number) => `${gib % 1 === 0 ? gib.toFixed(0) : gib.toFixed(1)} GiB of GPU memory`,
  context: (tokens: number) => `${tokens.toLocaleString("en-US")} tokens of context`,
  roles: (roles: readonly string[]) => (roles.length === 0 ? "serves no role" : `may serve ${roles.join(", ")}`),
  voterLine: (modelName: string, family: string) => `${modelName} (${family})`,
  voterCount: (n: number, families: number) =>
    `${n} ${n === 1 ? "voter" : "voters"} from ${families} model ${families === 1 ? "family" : "families"}.`,
  quant: { fp8: "FP8", awq4: "AWQ 4-bit", bf16: "BF16" } as Record<string, string>,
  footer:
    "Changes save to Models/models.yaml on the host; the model manager starts and stops instances to match within about 30 seconds, no restart needed.",

  /** "Add a model" (ADR-0018): a pasted link, the model fetcher, a card. */
  add: {
    heading: "Add a model",
    linkLabel: "Paste a Hugging Face link",
    linkPlaceholder: "https://huggingface.co/Qwen/Qwen3.8-27B-FP8",
    idLabel: "Registry id (optional)",
    idHelp: "Empty means the repository name in lowercase, made unique.",
    whatHappens:
      "The weights download onto this host through the model fetcher and the model appears on this page; give it a role to start it.",
    button: "Download and import",
    starting: "Starting…",
    notAllowed: "Adding a model needs the model:manage capability. Ask an administrator to add it, or to give you a role that includes it.",
    fetchesHeading: "Downloads",
    cancel: "Cancel",
    remove: "Remove",
    notStarted: {
      whatHappened: "The download didn't start.",
      likelyCause: "The api service didn't answer.",
      whatToDo: `Press Download and import again; ${LOGS_API}`,
    } satisfies ThreePart,
    notCancelled: {
      whatHappened: "The download wasn't cancelled.",
      likelyCause: "The api service didn't answer.",
      whatToDo: `Press Cancel again; ${LOGS_API}`,
    } satisfies ThreePart,
    progressLabel: (done: string, total: string) => `${done} of ${total} downloaded`,
    state: {
      planning: "Planning",
      downloading: "Downloading",
      importing: "Importing",
      done: "Done",
      failed: "Failed",
      cancelled: "Cancelled",
    } as Record<string, string>,
  },

  /** The Roles and Voters panels as a form (PUT /models/roles, INV-9). */
  edit: {
    noModel: "— no model —",
    notHere: (modelName: string) => `${modelName} (weights not here yet)`,
    voterHelp: "Pick models from different families so their errors decorrelate (CLAUDE.md §5.3).",
    save: "Save roles",
    saving: "Saving…",
    notAllowed: "Changing roles and voters needs the model:manage capability. Ask an administrator to change them, or to give you a role that includes it.",
    notSaved: {
      whatHappened: "The roles weren't saved.",
      likelyCause: "The api service didn't answer.",
      whatToDo: `Press Save roles again; ${LOGS_API}`,
    } satisfies ThreePart,
  },
};

// --- The agent pages on the real api (docs/api-contract-round-2.md §5, §7) -------------------
// Sentences the Http clients and the pages add around what the api sends. Everything the api
// says (run, job and task sentences, votes, findings, problems) is rendered as received.

export const agents = {
  /** The plan preview when the Consensus Router answered null (no voters configured). */
  notCrossChecked: "Not cross-checked: no voters are configured, so the plan needs your own review before it starts.",
  /** A finding the api sent without an owner; the sentence itself is still shown. */
  ownerUnknown: "Owner: not routed yet.",
  /** Under the Verdict heading while the voters' sentences are shown. */
  votesHeading: "What the voters said",
  /** The wizard's note after a spreadsheet was chosen (the server reads it; the browser cannot). */
  xlsxChosen: (filename: string, kib: number) =>
    `${filename} (${kib} KB) is read on the server. The items appear below once it has been parsed.`,
  /** The development fakes cannot read a spreadsheet. */
  xlsxNotInFake: (filename: string) =>
    `${filename} can't be read in this development build. Paste the suite as text, or run against the real api.`,
  /** When the api refused a push at the gate with a three-part answer instead of a report. */
  pushRefused: (parts: ThreePart) => [parts.whatHappened, parts.likelyCause, parts.whatToDo].filter((p) => p !== "").join(" "),
  /** Imported bundle branches, when the api reports them as names rather than sentences. */
  importedBranches: (names: readonly string[]) =>
    `Imported ${names.length} ${names.length === 1 ? "branch" : "branches"} under bundle/: ${names.join(", ")}.`,
  /** A station action the api refused; the page shows the three parts as one alert. */
  stationProblem: (parts: ThreePart) => [parts.whatHappened, parts.likelyCause, parts.whatToDo].filter((p) => p !== "").join(" "),
};
