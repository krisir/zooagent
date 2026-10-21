import { useEffect, useState } from "react";
import {
  Button,
  Card,
  Col,
  Dropdown,
  Form,
  Input,
  Modal,
  Row,
  Select,
  Space,
  Typography,
  message,
} from "antd";
import { MoreOutlined, PlusOutlined } from "@ant-design/icons";
import { api } from "../api/client";
import type { ModelProfile, ProviderPreset, SettingsPayload } from "../api/types";

interface SettingsModalProps {
  open: boolean;
  onClose: () => void;
  onChanged: () => void;
}

export function SettingsModal({ open, onClose, onChanged }: SettingsModalProps) {
  const [settings, setSettings] = useState<SettingsPayload | null>(null);
  const [editing, setEditing] = useState<ModelProfile | null>(null);
  const [form] = Form.useForm();
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState<string | null>(null);

  const load = async () => {
    try {
      setSettings(await api.getSettings());
    } catch (err) {
      message.error(err instanceof Error ? err.message : "读取设置失败");
    }
  };

  useEffect(() => {
    if (open) void load();
  }, [open]);

  const applyPreset = (preset: ProviderPreset) => {
    setEditing(null);
    form.setFieldsValue({
      id: undefined,
      name: preset.label,
      provider: preset.provider,
      model_family: preset.model_family ?? undefined,
      base_url: preset.base_url,
      model_name: preset.models[0] ?? "",
      api_key: "",
    });
  };

  const editProfile = (profile: ModelProfile) => {
    setEditing(profile);
    form.setFieldsValue({ ...profile, api_key: "" });
  };

  /** Back to "add" mode: an edit used to leave the form stuck on that profile. */
  const startCreate = () => {
    setEditing(null);
    form.resetFields();
    form.setFieldsValue({ provider: "agentscope" });
  };

  const save = async () => {
    try {
      const values = await form.validateFields();
      setSaving(true);
      const saved = await api.saveProfile({ ...values, id: editing?.id });
      await load();
      onChanged();
      // Stay on the saved profile, but as an edit: saving again updates it
      // instead of silently creating a duplicate.
      setEditing(saved.profile);
      form.setFieldsValue({ ...saved.profile, api_key: "" });
      message.success("已保存模型配置");
    } catch (err) {
      if (err instanceof Error && err.name !== "ValidationError") {
        message.error(err.message);
      }
    } finally {
      setSaving(false);
    }
  };

  const activate = async (profileId: string) => {
    try {
      await api.activateProfile(profileId);
      await load();
      onChanged();
      message.success("已切换当前模型");
    } catch (err) {
      message.error(err instanceof Error ? err.message : "切换失败");
    }
  };

  const test = async (profileId: string) => {
    setTesting(profileId);
    try {
      const result = await api.testProfile(profileId);
      if (result.ok) message.success(result.message);
      else message.error(result.message);
    } catch (err) {
      message.error(err instanceof Error ? err.message : "测试失败");
    } finally {
      setTesting(null);
    }
  };

  const remove = async (profileId: string) => {
    try {
      await api.deleteProfile(profileId);
      if (editing?.id === profileId) startCreate();
      await load();
      onChanged();
      message.success("已删除");
    } catch (err) {
      message.error(err instanceof Error ? err.message : "删除失败");
    }
  };

  return (
    <Modal
      open={open}
      onCancel={onClose}
      footer={null}
      width={860}
      title="设置"
      destroyOnClose
    >
      <div className="profile-list">
        {(settings?.profiles ?? []).map((profile) => {
          const active = settings?.active_model_profile_id === profile.id;
          const busy = testing === profile.id;
          return (
            <div
              key={profile.id}
              className={`profile-row${active ? " active" : ""}${editing?.id === profile.id ? " editing" : ""}`}
              onClick={() => editProfile(profile)}
            >
              <div className="profile-main">
                <div className="profile-name">
                  {profile.name}
                  {active ? <span className="profile-badge">使用中</span> : null}
                </div>
                <div className="profile-meta">
                  {profile.model_name}
                  {profile.provider === "offline"
                    ? ""
                    : profile.api_key_set
                      ? ` · 密钥 ····${profile.api_key_last4 ?? ""}`
                      : " · 未设置密钥"}
                </div>
              </div>
              <Space size={2} onClick={(event) => event.stopPropagation()}>
                {active ? null : (
                  <Button size="small" type="link" onClick={() => void activate(profile.id)}>
                    使用
                  </Button>
                )}
                <Dropdown
                  trigger={["click"]}
                  menu={{
                    items: [
                      { key: "test", label: busy ? "测试中…" : "测试连接", disabled: busy },
                      { key: "edit", label: "编辑" },
                      { type: "divider" },
                      { key: "delete", label: "删除", danger: true },
                    ],
                    onClick: ({ key, domEvent }) => {
                      domEvent.stopPropagation();
                      if (key === "test") void test(profile.id);
                      if (key === "edit") editProfile(profile);
                      if (key === "delete") void remove(profile.id);
                    },
                  }}
                >
                  <Button size="small" type="text" icon={<MoreOutlined />} />
                </Dropdown>
              </Space>
            </div>
          );
        })}

        {/* Always the last row: after editing a profile the form above is bound
            to it, so this is the one obvious way back to "add a new model". */}
        <div
          className={`profile-row add${editing ? "" : " current"}`}
          onClick={startCreate}
          role="button"
          tabIndex={0}
          onKeyDown={(event) => {
            if (event.key === "Enter" || event.key === " ") startCreate();
          }}
        >
          <div className="profile-main">
            <div className="profile-name">
              <PlusOutlined /> 添加模型
            </div>
            <div className="profile-meta">
              配置一个新的服务商，或手动填写 Base URL
            </div>
          </div>
        </div>
      </div>

      <Card
        size="small"
        title={editing ? `编辑：${editing.name}` : "添加模型"}
        style={{ marginTop: 16 }}
        extra={
          <Button size="small" onClick={() => void save()} loading={saving} type="primary">
            保存
          </Button>
        }
      >
        <Form form={form} layout="vertical" initialValues={{ provider: "agentscope" }}>
          <Row gutter={12}>
            {/* 协议不再暴露：预设决定 runtime/family，手填时统一走 AgentScope + URL 推断 */}
            <Form.Item name="provider" hidden>
              <Input />
            </Form.Item>
            <Form.Item name="model_family" hidden>
              <Input />
            </Form.Item>
            <Col span={24}>
              <Form.Item label="快速预设" style={{ marginBottom: 14 }}>
                <Select
                  allowClear
                  placeholder="选一个服务商自动填好；也可以直接在下面手填"
                  options={(settings?.providers ?? []).map((preset) => ({
                    label: preset.label,
                    value: preset.key,
                  }))}
                  onChange={(key: string | undefined) => {
                    if (!key) return;
                    const preset = settings?.providers.find((item) => item.key === key);
                    if (preset) applyPreset(preset);
                  }}
                />
              </Form.Item>
            </Col>
            <Col span={24}>
              <Form.Item name="name" label="名称" rules={[{ required: true }]}>
                <Input placeholder="例如：DeepSeek" />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name="base_url" label="Base URL">
                <Input placeholder="https://api.deepseek.com/v1" />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name="model_name" label="模型名称">
                <Input placeholder="deepseek-chat" />
              </Form.Item>
            </Col>
            <Col span={24}>
              <Form.Item name="api_key" label="API Key">
                <Input.Password
                  placeholder={
                    editing?.api_key_set
                      ? `已保存（····${editing.api_key_last4 ?? ""}），留空则不修改`
                      : "sk-..."
                  }
                />
              </Form.Item>
            </Col>
          </Row>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            模型需要支持 function calling（工具调用），否则主助手无法创建应用。
          </Typography.Text>
        </Form>
      </Card>
    </Modal>
  );
}
