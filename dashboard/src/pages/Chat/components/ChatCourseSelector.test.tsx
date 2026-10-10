import { App } from "antd";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  qixuebaoCoursesApi,
  type QixuebaoCourseList,
} from "../../../api/modules/qixuebaoCourses";
import { useChatCourseSelector } from "../hooks/useChatCourseSelector";
import ChatCourseSelector from "./ChatCourseSelector";

vi.mock("../../../api/modules/qixuebaoCourses", () => ({
  qixuebaoCoursesApi: { list: vi.fn(), select: vi.fn() },
}));

const courseList: QixuebaoCourseList = {
  courses: [
    { course_id: "safety", course_name: "食品安全培训" },
    { course_id: "service", course_name: "门店服务培训" },
    { course_id: "staff", course_name: "Staff Onboarding" },
  ],
  selected_courses: [],
  selected_course_ids: [],
  page_num: 1,
  page_size: 1000,
  pages: 1,
  total: 3,
};

function CourseSelector() {
  const selector = useChatCourseSelector("agent-a", null, true);
  return (
    <ChatCourseSelector
      courses={selector.courses}
      selectedCourseIds={selector.selectedCourseIds}
      loading={selector.loading}
      saving={selector.saving}
      error={selector.error}
      onSelect={(ids) => void selector.select(ids)}
      onRetry={selector.retry}
      isStreaming={false}
    />
  );
}

async function openSelector() {
  render(
    <App>
      <CourseSelector />
    </App>,
  );
  const input = screen.getByRole("combobox");
  await waitFor(() => expect(qixuebaoCoursesApi.list).toHaveBeenCalled());
  await waitFor(() => expect(input).not.toBeDisabled());
  const user = userEvent.setup();
  await user.click(input);
  const dropdown = within(
    document.querySelector(".ant-select-dropdown") as HTMLElement,
  );
  await dropdown.findByText("食品安全培训");
  return { input, user, dropdown };
}

describe("ChatCourseSelector search", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    vi.mocked(qixuebaoCoursesApi.list).mockResolvedValue(courseList);
  });

  it("filters by name immediately, keeps selected labels, and restores options when cleared", async () => {
    vi.mocked(qixuebaoCoursesApi.list).mockResolvedValue({
      ...courseList,
      selected_courses: [courseList.courses[0]],
      selected_course_ids: ["safety"],
    });
    const { input, user, dropdown } = await openSelector();

    await user.type(input, "服务");
    expect(dropdown.getByText("门店服务培训")).toBeInTheDocument();
    expect(dropdown.queryByText("食品安全培训")).not.toBeInTheDocument();
    expect(screen.getByText("食品安全培训")).toBeInTheDocument();
    expect(input).not.toBeDisabled();

    await user.clear(input);
    expect(dropdown.getByText("食品安全培训")).toBeInTheDocument();
    expect(dropdown.getByText("Staff Onboarding")).toBeInTheDocument();
    expect(qixuebaoCoursesApi.list).toHaveBeenCalledTimes(1);
  });

  it("searches course names case insensitively across all loaded pages", async () => {
    vi.mocked(qixuebaoCoursesApi.list)
      .mockResolvedValueOnce({
        ...courseList,
        courses: [courseList.courses[0]],
        pages: 2,
      })
      .mockResolvedValueOnce({
        ...courseList,
        courses: courseList.courses.slice(1),
        page_num: 2,
        pages: 2,
      });
    const { input, user, dropdown } = await openSelector();

    await user.type(input, "ONBOARD");
    expect(dropdown.getByText("Staff Onboarding")).toBeInTheDocument();
    expect(dropdown.queryByText("食品安全培训")).not.toBeInTheDocument();
    expect(qixuebaoCoursesApi.list).toHaveBeenCalledTimes(2);
    expect(qixuebaoCoursesApi.list).toHaveBeenLastCalledWith(
      "agent-a",
      null,
      2,
    );
  });

  it("keeps selections across searches and clears them with all courses", async () => {
    const { input, user, dropdown } = await openSelector();

    await user.type(input, "安全");
    await user.click(dropdown.getByText("食品安全培训"));
    await user.type(input, "服务");
    await user.click(dropdown.getByText("门店服务培训"));
    expect(
      screen.getByText("食品安全培训", {
        selector: ".ant-select-selection-item-content",
      }),
    ).toBeInTheDocument();
    expect(
      screen.getByText("门店服务培训", {
        selector: ".ant-select-selection-item-content",
      }),
    ).toBeInTheDocument();

    await user.click(dropdown.getByText("全部课程"));
    expect(
      document.querySelectorAll(".ant-select-selection-item"),
    ).toHaveLength(0);
    expect(qixuebaoCoursesApi.list).toHaveBeenCalledTimes(1);
  });
});
