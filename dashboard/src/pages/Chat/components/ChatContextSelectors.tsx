import type { QixuebaoCourse } from "../../../api/modules/qixuebaoCourses";
import ChatCourseSelector from "./ChatCourseSelector";
import ChatStoreSelector from "./ChatStoreSelector";

interface ChatContextSelectorsProps {
  agentId?: string | null;
  threadId?: string | null;
  selectors: string[];
  courses: QixuebaoCourse[];
  selectedCourseIds: string[];
  courseLoading: boolean;
  courseSaving: boolean;
  courseError: boolean;
  onCourseSearch: (keyword: string) => void;
  onCourseSelect: (ids: string[]) => void;
  onCourseRetry: () => void;
  isStreaming: boolean;
}

export default function ChatContextSelectors({
  agentId,
  threadId,
  selectors,
  courses,
  selectedCourseIds,
  courseLoading,
  courseSaving,
  courseError,
  onCourseSearch,
  onCourseSelect,
  onCourseRetry,
  isStreaming,
}: ChatContextSelectorsProps) {
  const storeEnabled = selectors.includes("xm_store");
  const courseEnabled = selectors.includes("qixuebao_course");
  if (!storeEnabled && !courseEnabled) return null;

  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        flexWrap: "wrap",
        gap: 8,
        padding: "8px 12px 0",
      }}
    >
      {storeEnabled && (
        <ChatStoreSelector
          agentId={agentId}
          threadId={threadId}
          isStreaming={isStreaming}
        />
      )}
      {courseEnabled && (
        <ChatCourseSelector
          courses={courses}
          selectedCourseIds={selectedCourseIds}
          loading={courseLoading}
          saving={courseSaving}
          error={courseError}
          onSearch={onCourseSearch}
          onSelect={onCourseSelect}
          onRetry={onCourseRetry}
          isStreaming={isStreaming}
        />
      )}
    </div>
  );
}
