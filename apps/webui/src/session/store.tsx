import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";

import { asApiError, onUnauthorized, type UnauthorizedReason } from "../api/http";
import type { Installation, Person, SessionApi } from "./api";

// Who is signed in, for the whole app (ADR-0009). Checked once on load with GET /me; any 401
// from any request brings the app back to Sign in, with the "session ended" sentence when the
// reason is `expired`. The password typed at sign-in is held in memory only while the person
// must change it, because POST /me/password needs it; it is never stored anywhere else.

export type SessionNotice = "signed-out" | "expired" | null;

export type SessionState =
  | { status: "checking" }
  | { status: "anonymous"; notice: SessionNotice }
  | { status: "signed-in"; person: Person };

export interface Session {
  state: SessionState;
  /** From the public endpoint; null until it answers (the sign-in page falls back to the product name). */
  installation: Installation | null;
  signIn(email: string, password: string): Promise<Person>;
  signOut(): Promise<void>;
  /** Uses the password remembered from sign-in unless `current` is given. */
  changePassword(next: string, current?: string): Promise<Person>;
  /** True when the remembered sign-in password is gone (after a reload) and must be typed again. */
  needsCurrentPassword: boolean;
  clearNotice(): void;
  hasCapability(capability: string): boolean;
}

const SessionContext = createContext<Session | null>(null);

/** The session, or null when the app runs ungated (component tests without a SessionApi). */
export function useSession(): Session | null {
  return useContext(SessionContext);
}

interface Props {
  api: SessionApi;
  children: React.ReactNode;
}

export function SessionProvider({ api, children }: Props) {
  const [state, setState] = useState<SessionState>({ status: "checking" });
  const [installation, setInstallation] = useState<Installation | null>(null);
  const [needsCurrentPassword, setNeedsCurrentPassword] = useState(false);
  const remembered = useRef<string | null>(null);
  // POST /me/password answers 401 for a wrong *current* password while the session itself is
  // still good; that 401 must show as an error on the page, not end the session.
  const changingPassword = useRef(false);

  useEffect(() => {
    let live = true;
    void api
      .installation()
      .then((value) => {
        if (live) {
          setInstallation(value);
        }
      })
      .catch(() => undefined);
    void api
      .me()
      .then((person) => {
        if (live) {
          setNeedsCurrentPassword(person.must_change_password);
          setState({ status: "signed-in", person });
        }
      })
      .catch((error: unknown) => {
        if (live) {
          const reason = asApiError(error).reason;
          setState({ status: "anonymous", notice: reason === "expired" ? "expired" : null });
        }
      });
    return () => {
      live = false;
    };
  }, [api]);

  useEffect(
    () =>
      onUnauthorized((reason: UnauthorizedReason) => {
        if (reason !== "expired" && changingPassword.current) {
          return;
        }
        remembered.current = null;
        setState((current) => {
          // A wrong password on the sign-in page is also a 401; it does not end anything.
          if (current.status === "anonymous" && reason !== "expired") {
            return current;
          }
          return { status: "anonymous", notice: reason === "expired" ? "expired" : null };
        });
      }),
    [],
  );

  const signIn = useCallback(
    async (email: string, password: string) => {
      const person = await api.signIn(email, password);
      remembered.current = person.must_change_password ? password : null;
      setNeedsCurrentPassword(false);
      setState({ status: "signed-in", person });
      return person;
    },
    [api],
  );

  const signOut = useCallback(async () => {
    remembered.current = null;
    try {
      await api.signOut();
    } finally {
      setState({ status: "anonymous", notice: "signed-out" });
    }
  }, [api]);

  const changePassword = useCallback(
    async (next: string, current?: string) => {
      changingPassword.current = true;
      try {
        const person = await api.changePassword(current ?? remembered.current ?? "", next);
        remembered.current = null;
        setNeedsCurrentPassword(false);
        setState({ status: "signed-in", person });
        return person;
      } finally {
        changingPassword.current = false;
      }
    },
    [api],
  );

  const clearNotice = useCallback(() => {
    setState((current) => (current.status === "anonymous" && current.notice !== null ? { status: "anonymous", notice: null } : current));
  }, []);

  const value = useMemo<Session>(
    () => ({
      state,
      installation,
      signIn,
      signOut,
      changePassword,
      needsCurrentPassword,
      clearNotice,
      hasCapability: (capability) => state.status === "signed-in" && state.person.capabilities.includes(capability),
    }),
    [state, installation, signIn, signOut, changePassword, needsCurrentPassword, clearNotice],
  );

  return <SessionContext.Provider value={value}>{children}</SessionContext.Provider>;
}
