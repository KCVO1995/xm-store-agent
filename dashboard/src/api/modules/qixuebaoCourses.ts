import { request } from "../request";

export interface QixuebaoCourse {
  course_id: string;
  course_name: string;
}

export interface QixuebaoCourseList {
  courses: QixuebaoCourse[];
  selected_courses: QixuebaoCourse[];
  selected_course_ids: string[];
  page_num: number;
  page_size: number;
  pages: number;
  total: number;
}

const path = (agentId: string) =>
  `/agents/${encodeURIComponent(agentId)}/chat-context/courses`;

export const qixuebaoCoursesApi = {
  list: (agentId: string, threadId?: string | null, pageNum = 1) => {
    const params = new URLSearchParams({ page_num: String(pageNum) });
    if (threadId) params.set("thread_id", threadId);
    return request<QixuebaoCourseList>(`${path(agentId)}?${params}`);
  },
  select: (agentId: string, threadId: string, courseIds: string[]) =>
    request<{ selected_course_ids: string[] }>(`${path(agentId)}/selection`, {
      method: "PUT",
      body: JSON.stringify({ thread_id: threadId, course_ids: courseIds }),
    }),
};
