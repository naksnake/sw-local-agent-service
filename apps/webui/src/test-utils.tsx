// Helpers for the component tests: the App inside a memory router, and a fake world with a
// person already signed in.

import { render } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import { App, type AppProps } from "./App";
import { type FakeAccount, FakeSessionApi, FakeWorld } from "./session/fake";

export function renderApp(props: AppProps = {}, route = "/") {
  return render(
    <MemoryRouter initialEntries={[route]}>
      <App {...props} />
    </MemoryRouter>,
  );
}

/** A fake session with `email` signed in and no password change pending. */
export function signedIn(email = "pat@slas.local", world = new FakeWorld()): { world: FakeWorld; sessionApi: FakeSessionApi; account: FakeAccount } {
  const account = world.byEmail(email);
  if (account === undefined) {
    throw new Error(`${email} is not in the fake world`);
  }
  account.must_change_password = false;
  const sessionApi = new FakeSessionApi(world);
  sessionApi.current = account;
  return { world, sessionApi, account };
}
