import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Form, Select } from "antd";

import { connectorsApi } from "../../../api/modules/connectors";
import { knowledgeBasesApi } from "../../../api/modules/knowledgeBases";
import { useCurrentUser } from "../../../hooks/useCurrentUser";

export default function ExpertComposerDefaultsFields() {
  const { t } = useTranslation();
  const currentUserId = useCurrentUser()?.id ?? null;
  const [knowledgeBases, setKnowledgeBases] = useState<
    { id: string; name: string }[]
  >([]);
  const [knowledgeLoading, setKnowledgeLoading] = useState(false);
  const [connectors, setConnectors] = useState<
    { value: string; label: string }[]
  >([]);
  const [connectorsLoading, setConnectorsLoading] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setKnowledgeLoading(true);
    knowledgeBasesApi
      .list()
      .then((bases) => {
        if (!cancelled) {
          setKnowledgeBases(
            bases.map((base) => ({ id: base.id, name: base.name })),
          );
        }
      })
      .catch(() => {
        if (!cancelled) setKnowledgeBases([]);
      })
      .finally(() => {
        if (!cancelled) setKnowledgeLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    setConnectorsLoading(true);
    connectorsApi
      .listInstances()
      .then((instances) => {
        if (cancelled) return;
        setConnectors(
          (instances ?? [])
            .filter((item) => item.status === "active" && item.has_credentials)
            .map((item) => ({
              value: item.mcp_server_name,
              label:
                currentUserId !== null && item.owner_user_id !== currentUserId
                  ? `${item.display_name} · ${
                      item.owner_display_name ||
                      item.owner_username ||
                      item.owner_user_id
                    }`
                  : item.display_name || item.mcp_server_name,
            })),
        );
      })
      .catch(() => {
        if (!cancelled) setConnectors([]);
      })
      .finally(() => {
        if (!cancelled) setConnectorsLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [currentUserId]);

  return (
    <>
      <Form.Item
        name="knowledge_base_ids"
        label={t("experts.knowledgeBasesLabel")}
        extra={t("experts.knowledgeBasesHint")}
      >
        <Select
          mode="multiple"
          allowClear
          showSearch
          optionFilterProp="label"
          loading={knowledgeLoading}
          options={knowledgeBases.map((base) => ({
            value: base.id,
            label: base.name,
          }))}
          placeholder={t("experts.knowledgeBasesPlaceholder")}
        />
      </Form.Item>
      <Form.Item
        name="mcp_servers"
        label={t("experts.connectorsLabel")}
        extra={t("experts.connectorsHint")}
      >
        <Select
          mode="multiple"
          allowClear
          showSearch
          optionFilterProp="label"
          loading={connectorsLoading}
          options={connectors}
          placeholder={t("experts.connectorsPlaceholder")}
        />
      </Form.Item>
      <Form.Item
        name="chat_selectors"
        label={t("experts.chatSelectorsLabel", "聊天选项")}
        extra={t("experts.chatSelectorsHint", "选择此专家聊天窗显示的业务选项")}
      >
        <Select
          mode="multiple"
          allowClear
          options={[
            { value: "xm_store", label: t("chat.storeSelector", "门店") },
            {
              value: "qixuebao_course",
              label: t("chat.courseSelector", "企学宝课程"),
            },
          ]}
          placeholder={t("experts.chatSelectorsPlaceholder", "选择聊天选项")}
        />
      </Form.Item>
    </>
  );
}
