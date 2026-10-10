import { useTranslation } from "react-i18next";
import { Button, Select } from "antd";

import type { QixuebaoCourse } from "../../../api/modules/qixuebaoCourses";

interface ChatCourseSelectorProps {
  courses: QixuebaoCourse[];
  selectedCourseIds: string[];
  loading: boolean;
  saving: boolean;
  error: boolean;
  onSearch: (keyword: string) => void;
  onSelect: (ids: string[]) => void;
  onRetry: () => void;
  isStreaming: boolean;
}

export default function ChatCourseSelector({
  courses,
  selectedCourseIds,
  loading,
  saving,
  error,
  onSearch,
  onSelect,
  onRetry,
  isStreaming,
}: ChatCourseSelectorProps) {
  const { t } = useTranslation();
  return (
    <>
      <span
        style={{
          whiteSpace: "nowrap",
          fontSize: 12,
          color: "var(--fn-text-tertiary)",
        }}
      >
        {t("chat.courseSelector", "企学宝课程")}
      </span>
      {error ? (
        <Button size="small" onClick={onRetry}>
          {t("chat.courseLoadRetry", "课程加载失败，重试")}
        </Button>
      ) : (
        <Select
          mode="multiple"
          showSearch
          allowClear
          filterOption={false}
          loading={loading || saving}
          disabled={loading || saving || isStreaming}
          style={{ minWidth: 220, maxWidth: "100%" }}
          placeholder={t("chat.courseAll", "全部课程")}
          value={selectedCourseIds}
          options={[
            { value: "__all__", label: t("chat.courseAll", "全部课程") },
            ...courses.map((course) => ({
              value: course.course_id,
              label: course.course_name,
            })),
          ]}
          onSearch={onSearch}
          onChange={(values) =>
            onSelect(values.includes("__all__") ? [] : values)
          }
        />
      )}
    </>
  );
}
