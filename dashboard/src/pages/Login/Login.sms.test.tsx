import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

const { sendCode, loginCode, loginPassword } = vi.hoisted(() => ({
  sendCode: vi.fn().mockResolvedValue(undefined),
  loginCode: vi.fn().mockResolvedValue({
    access_token: "octop-token",
    user: { locale: "zh" },
  }),
  loginPassword: vi.fn(),
}));

vi.mock("../../context/ThemeContext", () => ({
  useTheme: () => ({ isDark: false }),
}));

vi.mock("../../api/modules/auth", () => ({
  authApi: {
    getAuthStatus: () => Promise.resolve({ setup_required: false }),
    getOauthStatus: () => Promise.resolve({ providers: [] }),
    getCaptcha: () => Promise.resolve({ provider: "slider" }),
    sendXmStoreCode: sendCode,
    loginXmStoreByCode: loginCode,
    loginXmStore: loginPassword,
  },
}));

vi.mock("../../utils/locale", () => ({
  applyGuestLocale: () => Promise.resolve(),
  applyUserLocale: () => Promise.resolve(),
}));

import LoginPage from "./index";

describe("company SMS login", () => {
  it("defaults to SMS, permits repeat sends, and retains password login", async () => {
    const user = userEvent.setup();
    render(
      <MemoryRouter>
        <LoginPage />
      </MemoryRouter>,
    );

    const send = screen.getByTestId("login-company-send-code");
    expect(send).toBeDisabled();
    await user.type(screen.getByTestId("login-company-phone"), "13812345678");
    await user.click(send);
    await waitFor(() =>
      expect(sendCode).toHaveBeenCalledWith("86", "13812345678"),
    );
    await user.click(send);
    await waitFor(() => expect(sendCode).toHaveBeenCalledTimes(2));

    await user.type(screen.getByTestId("login-company-sms-code"), "123456");
    await user.click(screen.getByTestId("login-submit"));
    await waitFor(() =>
      expect(loginCode).toHaveBeenCalledWith("86", "13812345678", "123456"),
    );

    await user.click(screen.getByTestId("login-company-password-toggle"));
    expect(screen.queryByTestId("login-company-phone")).toBeNull();
    expect(
      screen.getByPlaceholderText(/companyUsername|公司账号|Company account/),
    ).toBeInTheDocument();
  });
});
