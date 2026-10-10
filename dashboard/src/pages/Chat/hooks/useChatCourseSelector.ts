import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { App } from "antd";
import {
  qixuebaoCoursesApi,
  type QixuebaoCourse,
} from "../../../api/modules/qixuebaoCourses";
import { isPendingThread } from "./useSessions";

export function useChatCourseSelector(
  agentId: string | null | undefined,
  threadId: string | null,
  enabled: boolean,
) {
  const { t } = useTranslation();
  const { message } = App.useApp();
  const [courses, setCourses] = useState<QixuebaoCourse[]>([]);
  const [selectedCourseIds, setSelectedCourseIds] = useState<string[]>([]);
  const [keyword, setKeyword] = useState("");
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(false);
  const [retry, setRetry] = useState(0);
  const [ready, setReady] = useState(false);
  const hydratedKey = useRef("");
  const selectedIdsRef = useRef<string[]>([]);
  const pendingAgentRef = useRef<string | null>(null);

  const persistedThreadId =
    threadId && !isPendingThread(threadId) ? threadId : null;
  const selectionKey = `${agentId ?? ""}\0${persistedThreadId ?? ""}`;

  useEffect(() => {
    if (threadId && isPendingThread(threadId))
      pendingAgentRef.current = agentId ?? null;
  }, [agentId, threadId]);

  useEffect(() => {
    const carryDraft =
      Boolean(persistedThreadId) && pendingAgentRef.current === agentId;
    const carriedIds = carryDraft ? selectedIdsRef.current : [];
    if (!carryDraft) setCourses([]);
    setSelectedCourseIds(carriedIds);
    setKeyword("");
    setError(false);
    setReady(carryDraft);
    hydratedKey.current = carryDraft ? selectionKey : "";
    selectedIdsRef.current = carriedIds;
    pendingAgentRef.current = null;
  }, [agentId, persistedThreadId, enabled, selectionKey]);

  useEffect(() => {
    if (!enabled || !agentId) return;
    let cancelled = false;
    const timer = setTimeout(
      () => {
        setLoading(true);
        void qixuebaoCoursesApi
          .list(agentId, persistedThreadId, keyword)
          .then((result) => {
            if (cancelled) return;
            setCourses((previous) => {
              const merged = new Map(
                [
                  ...previous.filter((course) =>
                    selectedIdsRef.current.includes(course.course_id),
                  ),
                  ...result.selected_courses,
                  ...result.courses,
                ].map((course) => [course.course_id, course]),
              );
              return [...merged.values()];
            });
            if (hydratedKey.current !== selectionKey) {
              setSelectedCourseIds(result.selected_course_ids);
              selectedIdsRef.current = result.selected_course_ids;
              hydratedKey.current = selectionKey;
              setReady(true);
            }
            setError(false);
          })
          .catch(() => {
            if (!cancelled) setError(true);
          })
          .finally(() => {
            if (!cancelled) setLoading(false);
          });
      },
      keyword ? 250 : 0,
    );
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [agentId, persistedThreadId, enabled, keyword, retry, selectionKey]);

  const select = useCallback(
    async (ids: string[]) => {
      if (!agentId || !enabled || saving) return;
      if (!persistedThreadId) {
        setSelectedCourseIds(ids);
        selectedIdsRef.current = ids;
        return;
      }
      setSaving(true);
      try {
        const result = await qixuebaoCoursesApi.select(
          agentId,
          persistedThreadId,
          ids,
        );
        setSelectedCourseIds(result.selected_course_ids);
        selectedIdsRef.current = result.selected_course_ids;
      } catch {
        message.error(t("chat.courseSelectFailed", "课程选择失败，请重试"));
      } finally {
        setSaving(false);
      }
    },
    [agentId, enabled, persistedThreadId, saving, message, t],
  );

  return {
    courses,
    selectedCourseIds,
    ready: ready && hydratedKey.current === selectionKey,
    loading,
    saving,
    error,
    select,
    search: setKeyword,
    retry: () => setRetry((value) => value + 1),
  };
}
