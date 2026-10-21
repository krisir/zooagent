import { useEffect, useState } from "react";
import type { ReactNode } from "react";
import {
  Alert,
  Button,
  Calendar,
  Card,
  Col,
  DatePicker,
  Divider as AntDivider,
  Empty,
  Form,
  Input,
  InputNumber,
  Modal,
  Popconfirm,
  Row,
  Select,
  Space,
  Statistic,
  Switch,
  Table,
  Tabs,
  Tag,
  Typography,
  message,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { DeleteOutlined, EditOutlined, PlusOutlined, SearchOutlined } from "@ant-design/icons";
import dayjs, { type Dayjs } from "dayjs";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Legend,
  Line,
  LineChart,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip as ReTooltip,
  XAxis,
  YAxis,
} from "recharts";
import { aggregateAlias, api, ApiError, firstDateField, firstTextField, optionList } from "../api/client";
import type {
  ActionDef,
  FieldDef,
  Filter,
  ModelDef,
  RecordItem,
  UIComponent,
} from "../api/types";
import { CHART_COLORS } from "../theme";
import { ChatPanel } from "./ChatPanel";

/** More slices than this and a pie chart stops being readable. */
const MAX_PIE_SLICES = 8;

interface RendererProps {
  appId: string;
  appName?: string;
  models: ModelDef[];
  actions: ActionDef[];
  components: UIComponent[];
  layout?: "vertical" | "grid" | "two_column";
  refreshToken: number;
  onChanged: () => void;
}

export function Renderer({
  appId,
  appName,
  models,
  actions,
  components,
  layout = "vertical",
  refreshToken,
  onChanged,
}: RendererProps) {
  const [search, setSearch] = useState("");

  // A leading heading that just repeats the application name is noise: the
  // page header already shows it.
  const visible = components.filter((component, index) => {
    if (index !== 0 || !appName) return true;
    return !(component.type === "Heading" && (component.title ?? "").trim() === appName.trim());
  });

  const render = (component: UIComponent, index: number) => (
    <ComponentView
      key={component.id ?? `${component.type}-${index}`}
      appId={appId}
      component={component}
      models={models}
      actions={actions}
      refreshToken={refreshToken}
      onChanged={onChanged}
      search={search}
      onSearch={setSearch}
    />
  );

  const nodes = visible.map(render);

  if (layout === "grid") {
    return (
      <Row gutter={[16, 16]}>
        {nodes.map((node, index) => (
          <Col key={index} xs={24} md={12}>
            {node}
          </Col>
        ))}
      </Row>
    );
  }
  if (layout === "two_column") {
    return (
      <Row gutter={[16, 16]}>
        <Col xs={24} lg={14}>
          {nodes.filter((_, index) => index % 2 === 0).map((node, index) => (
            <div key={index} style={{ marginBottom: 16 }}>
              {node}
            </div>
          ))}
        </Col>
        <Col xs={24} lg={10}>
          {nodes.filter((_, index) => index % 2 === 1).map((node, index) => (
            <div key={index} style={{ marginBottom: 16 }}>
              {node}
            </div>
          ))}
        </Col>
      </Row>
    );
  }

  // Vertical pages read better when consecutive metric cards share a row
  // instead of each stretching the full width.
  const blocks: ReactNode[] = [];
  let stats: UIComponent[] = [];
  const flushStats = () => {
    if (!stats.length) return;
    const group = stats;
    stats = [];
    blocks.push(
      <div className="stat-row" key={`stats-${blocks.length}`}>
        {group.map((component) =>
          render(component, visible.indexOf(component)),
        )}
      </div>,
    );
  };
  visible.forEach((component, index) => {
    if (component.type === "StatCard") {
      stats.push(component);
      return;
    }
    flushStats();
    blocks.push(render(component, index));
  });
  flushStats();

  return (
    <div className="render-vertical">
      {blocks}
    </div>
  );
}

interface ComponentProps {
  appId: string;
  component: UIComponent;
  models: ModelDef[];
  actions: ActionDef[];
  refreshToken: number;
  onChanged: () => void;
  search: string;
  onSearch: (value: string) => void;
}

function ComponentView(props: ComponentProps) {
  const { component, models } = props;
  const model = component.model ? models.find((item) => item.key === component.model) : undefined;

  switch (component.type) {
    case "Heading":
      return <Typography.Title level={(Number(component.props?.level) || 3) as 1 | 2 | 3 | 4 | 5}>{component.title}</Typography.Title>;
    case "Text":
      return (
        <Typography.Paragraph type="secondary" style={{ marginBottom: 0 }}>
          {String(component.props?.text ?? component.description ?? "")}
        </Typography.Paragraph>
      );
    case "Divider":
      return <AntDivider style={{ margin: "4px 0" }} />;
    case "DataTable":
      if (!model) return <MissingModel component={component} />;
      return <DataTable {...props} model={model} />;
    case "StatCard":
      if (!model) return <MissingModel component={component} />;
      return <StatCard {...props} model={model} />;
    case "Chart":
      if (!model) return <MissingModel component={component} />;
      return <ChartCard {...props} model={model} />;
    case "DetailView":
      if (!model) return <MissingModel component={component} />;
      return <DetailView {...props} model={model} />;
    case "Form":
      if (!model) return <MissingModel component={component} />;
      return <InlineFormCard {...props} model={model} />;
    case "Calendar":
      if (!model) return <MissingModel component={component} />;
      return <CalendarCard {...props} model={model} />;
    case "Tabs":
      return <TabsCard {...props} />;
    case "FilterPanel":
      return <FilterPanel {...props} />;
    case "ActionButton":
      return <ActionButtons {...props} />;
    case "ChatPanel":
      return (
        <Card size="small" title={component.title ?? "助手"}>
          <div style={{ height: 420 }}>
            <ChatPanel
              applicationId={props.appId}
              title={component.title ?? "助手"}
              hint={component.description ?? undefined}
              onChanged={props.onChanged}
            />
          </div>
        </Card>
      );
    default:
      return (
        <Alert
          type="warning"
          showIcon
          message={`未知组件类型：${component.type}`}
          description="请让主助手重新生成界面。"
        />
      );
  }
}

function MissingModel({ component }: { component: UIComponent }) {
  return (
    <Alert
      type="warning"
      showIcon
      message={`${component.type} 引用的模型不存在`}
      description={`model = ${component.model ?? "(未指定)"}`}
    />
  );
}

function panelProps(component: UIComponent, model: ModelDef): Filter[] {
  const props = component.props ?? {};
  const filters = [...((props.filters as Filter[]) ?? [])];
  const period = props.period;
  if (period && period !== "all") {
    const dateField =
      (props.date_field as string | undefined) ?? firstDateField(model)?.key;
    if (dateField) {
      const now = dayjs();
      const start = period === "this_year" ? now.startOf("year") : now.startOf("month");
      const end = period === "this_year" ? now.endOf("year") : now.endOf("month");
      filters.push({
        field: dateField,
        operator: "between",
        value: [start.format("YYYY-MM-DD"), end.format("YYYY-MM-DD")],
      });
    }
  }
  return filters;
}

function StatCard({ appId, component, model, refreshToken }: ComponentProps & { model: ModelDef }) {
  const [value, setValue] = useState<number | null>(null);
  const [loading, setLoading] = useState(false);
  const props = component.props ?? {};
  const aggregate = (props.aggregate as { function: string; field?: string; alias?: string }) ?? {
    function: "count",
  };
  const alias = aggregateAlias(aggregate);
  const format = props.format ?? "number";

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    api
      .aggregate(appId, model.key, {
        filters: panelProps(component, model),
        aggregations: [aggregate],
      })
      .then((result) => {
        if (cancelled) return;
        const raw = result.items[0]?.[alias];
        setValue(raw === null || raw === undefined ? 0 : Number(raw));
      })
      .catch(() => {
        if (!cancelled) setValue(null);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [appId, component, model, alias, refreshToken]);

  return (
    <Card size="small">
      <Statistic
        title={component.title ?? model.name}
        value={value ?? 0}
        precision={format === "currency" ? 2 : 0}
        prefix={format === "currency" ? "¥" : undefined}
        loading={loading}
      />
      {component.description ? (
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          {component.description}
        </Typography.Text>
      ) : null}
    </Card>
  );
}

function ChartCard({
  appId,
  component,
  model,
  models,
  refreshToken,
}: ComponentProps & { model: ModelDef }) {
  const [data, setData] = useState<{ name: string; value: number }[]>([]);
  const [loading, setLoading] = useState(false);
  const props = component.props ?? {};
  const chartType = (props.chart_type as string) ?? "bar";
  const groupBy = ((props.group_by as string[]) ?? [])[0] ?? firstTextField(model)?.key;
  const granularity = (props.granularity as string | undefined) ?? undefined;
  const aggregate =
    (props.aggregate as { function: string; field?: string; alias?: string }) ?? {
      function: "count",
    };
  const alias = aggregateAlias(aggregate);

  useEffect(() => {
    if (!groupBy) return;
    let cancelled = false;
    setLoading(true);
    api
      .aggregate(appId, model.key, {
        filters: panelProps(component, model),
        group_by: [groupBy],
        granularity,
        // trends must read left to right
        sort: granularity ? [{ field: groupBy, direction: "asc" }] : [],
        aggregations: [{ ...aggregate, alias }],
      })
      .then(async (result) => {
        if (cancelled) return;
        const field = model.fields.find((item) => item.key === groupBy);
        let labelMap: Record<string, string> = {};
        if (field?.type === "relation" && field.target_model) {
          const target = models.find((item) => item.key === field.target_model);
          if (target) {
            const related = await api.queryRecords(appId, target.key, { limit: 500 });
            labelMap = Object.fromEntries(related.items.map((item) => [item.id, item.title]));
          }
        }
        if (cancelled) return;
        const mapped = result.items.map((item) => ({
          name: labelMap[String(item[groupBy])] ?? String(item[groupBy] ?? "未分类"),
          value: Number(item[alias] ?? 0),
        }));
        // A pie with 40 slices is unreadable, so keep the biggest ones and
        // fold the rest into "其他".
        const sorted = [...mapped].sort((a, b) => b.value - a.value);
        if (chartType === "pie" && sorted.length > MAX_PIE_SLICES) {
          const head = sorted.slice(0, MAX_PIE_SLICES);
          const tail = sorted.slice(MAX_PIE_SLICES);
          head.push({
            name: `其他（${tail.length} 项）`,
            value: tail.reduce((sum, item) => sum + item.value, 0),
          });
          setData(head);
        } else {
          setData(sorted.length ? sorted : mapped);
        }
      })
      .catch(() => {
        if (!cancelled) setData([]);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [appId, model, component, groupBy, alias, granularity, refreshToken]);

  const tooltip = <ReTooltip formatter={(value: number) => value.toLocaleString()} />;

  return (
    <Card size="small" title={component.title ?? `${model.name}统计`} loading={loading}>
      {data.length === 0 ? (
        <Empty description="暂无数据" image={Empty.PRESENTED_IMAGE_SIMPLE} />
      ) : (
        <div style={{ height: 260 }}>
          <ResponsiveContainer width="100%" height="100%">
            {chartType === "pie" ? (
              <PieChart>
                <Pie
                  data={data}
                  dataKey="value"
                  nameKey="name"
                  outerRadius={90}
                  label={data.length <= 5}
                >
                  {data.map((_entry, index) => (
                    <Cell key={index} fill={CHART_COLORS[index % CHART_COLORS.length]} />
                  ))}
                </Pie>
                {tooltip}
                <Legend />
              </PieChart>
            ) : chartType === "line" ? (
              <LineChart data={data}>
                <CartesianGrid strokeDasharray="3 3" />
                <XAxis dataKey="name" />
                <YAxis />
                {tooltip}
                <Line type="monotone" dataKey="value" stroke="#1677ff" strokeWidth={2} />
              </LineChart>
            ) : (
              <BarChart data={data}>
                <CartesianGrid strokeDasharray="3 3" />
                <XAxis dataKey="name" />
                <YAxis />
                {tooltip}
                <Bar dataKey="value" fill="#1677ff" radius={[4, 4, 0, 0]} />
              </BarChart>
            )}
          </ResponsiveContainer>
        </div>
      )}
      {data.length > 0 ? (
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          {chartType === "pie" ? "占比" : "分布"}：{groupBy}
        </Typography.Text>
      ) : null}
    </Card>
  );
}

function ValueCell({ field, record }: { field: FieldDef; record: RecordItem }) {
  const value = record.data[field.key];
  if (field.type === "relation") {
    const label = record.labels?.[field.key];
    if (Array.isArray(label)) return <span>{label.join("、")}</span>;
    if (label) return <span>{label}</span>;
    return <Typography.Text type="secondary">—</Typography.Text>;
  }
  if (value === null || value === undefined || value === "") {
    return <Typography.Text type="secondary">—</Typography.Text>;
  }
  if (field.type === "enum") {
    const option = optionList(field).find((item) => item.value === String(value));
    return <Tag bordered={false}>{option?.label ?? String(value)}</Tag>;
  }
  if (field.type === "boolean") {
    return value ? <Tag color="green" bordered={false}>是</Tag> : <Tag bordered={false}>否</Tag>;
  }
  if (field.type === "decimal") {
    return <span>{Number(value).toFixed(field.scale ?? 2)}</span>;
  }
  if (field.type === "json") {
    return <code style={{ fontSize: 12 }}>{JSON.stringify(value)}</code>;
  }
  return <span>{String(value)}</span>;
}

function DataTable({
  appId,
  component,
  model,
  models,
  actions,
  refreshToken,
  onChanged,
  search,
}: ComponentProps & { model: ModelDef }) {
  const props = component.props ?? {};
  const pageSize = Number(props.page_size ?? 10);
  const editable = props.editable !== false;
  const [rows, setRows] = useState<RecordItem[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [sort, setSort] = useState<{ field: string; direction: "asc" | "desc" } | null>(null);
  const [loading, setLoading] = useState(false);
  const [formOpen, setFormOpen] = useState(false);
  const [editing, setEditing] = useState<RecordItem | null>(null);
  const [runningAction, setRunningAction] = useState<string | null>(null);
  const [localSearch, setLocalSearch] = useState("");

  const searchText = search || localSearch;
  const searchField = firstTextField(model)?.key;

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    const filters: Filter[] = [];
    if (searchText && searchField) {
      filters.push({ field: searchField, operator: "contains", value: searchText });
    }
    api
      .queryRecords(appId, model.key, {
        filters,
        sort: sort ? [sort] : [],
        limit: pageSize,
        offset: (page - 1) * pageSize,
      })
      .then((result) => {
        if (cancelled) return;
        setRows(result.items);
        setTotal(result.total);
      })
      .catch((err: Error) => {
        if (!cancelled) message.error(err.message);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [appId, model.key, page, pageSize, sort, searchText, searchField, refreshToken]);

  const columns: ColumnsType<RecordItem> = [];
  const selected =
    component.columns && component.columns.length
      ? component.columns
          .map((column) => model.fields.find((field) => field.key === column.field))
          .filter((field): field is FieldDef => Boolean(field))
      : model.fields.filter((field) => !field.hidden);

  for (const field of selected) {
    columns.push({
      title: field.name,
      key: field.key,
      sorter: true,
      render: (_: unknown, record: RecordItem) => <ValueCell field={field} record={record} />,
    });
  }
  if (editable) {
    columns.push({
      title: "",
      key: "actions",
      width: 96,
      render: (_: unknown, record: RecordItem) => (
        <Space size={4}>
          <Button
            type="text"
            size="small"
            icon={<EditOutlined />}
            onClick={() => {
              setEditing(record);
              setFormOpen(true);
            }}
          />
          <Popconfirm
            title="删除这条记录？"
            description="数据会软删除，可在审计记录中恢复。"
            okText="删除"
            cancelText="取消"
            onConfirm={async () => {
              try {
                await api.deleteRecord(appId, model.key, record.id);
                message.success("已删除");
                setPage(1);
                onChanged();
              } catch (err) {
                message.error(err instanceof Error ? err.message : "删除失败");
              }
            }}
          >
            <Button type="text" size="small" danger icon={<DeleteOutlined />} />
          </Popconfirm>
        </Space>
      ),
    });
  }

  const tableActions = component.actions ?? [];
  const runAction = actions.find((action) => action.key === runningAction);

  return (
    <Card
      size="small"
      title={
        <Space>
          <span>{component.title ?? model.name}</span>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            共 {total} 条
          </Typography.Text>
        </Space>
      }
      extra={
        <Space wrap>
          {searchField ? (
            <Input
              allowClear
              size="small"
              prefix={<SearchOutlined />}
              placeholder="搜索"
              style={{ width: 160 }}
              onChange={(event) => {
                setPage(1);
                setLocalSearch(event.target.value);
              }}
            />
          ) : null}
          {tableActions.map((action) => (
            <Button
              key={action.label}
              size="small"
              type={action.kind === "create" ? "primary" : "default"}
              icon={action.kind === "create" ? <PlusOutlined /> : undefined}
              onClick={() => {
                if (action.kind === "run_action" && action.action) {
                  setRunningAction(action.action);
                } else {
                  setEditing(null);
                  setFormOpen(true);
                }
              }}
            >
              {action.label}
            </Button>
          ))}
        </Space>
      }
    >
      <Table<RecordItem>
        size="small"
        rowKey="id"
        loading={loading}
        dataSource={rows}
        columns={columns}
        pagination={{
          current: page,
          pageSize,
          total,
          showSizeChanger: false,
          onChange: setPage,
        }}
        onChange={(_pagination, _filters, sorter) => {
          const current = Array.isArray(sorter) ? sorter[0] : sorter;
          if (current?.columnKey && current.order) {
            setSort({
              field: String(current.columnKey),
              direction: current.order === "ascend" ? "asc" : "desc",
            });
          } else {
            setSort(null);
          }
          setPage(1);
        }}
      />

      <RecordFormModal
        open={formOpen}
        appId={appId}
        model={model}
        models={models}
        record={editing}
        onClose={() => setFormOpen(false)}
        onSaved={() => {
          setFormOpen(false);
          message.success("已保存");
          onChanged();
        }}
      />

      {runAction ? (
        <ActionRunModal
          appId={appId}
          action={runAction}
          models={models}
          onClose={() => setRunningAction(null)}
          onDone={() => {
            setRunningAction(null);
            message.success("操作已完成");
            onChanged();
          }}
        />
      ) : null}
    </Card>
  );
}

function filterModelFields(model: ModelDef) {
  return model.fields.filter((field) => !field.hidden);
}

function toFormValue(field: FieldDef, value: unknown): unknown {
  if (value === null || value === undefined || value === "") return undefined;
  if (field.type === "date" || field.type === "datetime") return dayjs(String(value));
  if (field.type === "decimal" || field.type === "integer") return Number(value);
  return value;
}

function fromFormValue(field: FieldDef, value: unknown): unknown {
  if (value === undefined || value === null) return undefined;
  if (field.type === "date") return (value as Dayjs).format("YYYY-MM-DD");
  if (field.type === "datetime") return (value as Dayjs).toISOString();
  if (field.type === "decimal") return String(value);
  if (field.type === "integer") return Number(value);
  return value;
}

function FieldInput({
  field,
  models,
  appId,
  value,
  onChange,
}: {
  field: FieldDef;
  models: ModelDef[];
  appId: string;
  value?: unknown;
  onChange?: (value: unknown) => void;
}) {
  const common = { style: { width: "100%" } };
  if (field.type === "enum") {
    return (
      <Select
        {...common}
        value={value as string | undefined}
        onChange={onChange}
        options={optionList(field)}
        allowClear
        placeholder={`选择${field.name}`}
      />
    );
  }
  if (field.type === "relation" && field.target_model) {
    return (
      <RelationSelect
        field={field}
        models={models}
        appId={appId}
        value={value}
        onChange={onChange}
      />
    );
  }
  if (field.type === "boolean") {
    return <Switch checked={Boolean(value)} onChange={onChange} />;
  }
  if (field.type === "date") {
    return <DatePicker {...common} value={value as Dayjs | undefined} onChange={onChange} />;
  }
  if (field.type === "datetime") {
    return (
      <DatePicker {...common} showTime value={value as Dayjs | undefined} onChange={onChange} />
    );
  }
  if (field.type === "integer") {
    return <InputNumber {...common} value={value as number | undefined} onChange={onChange} precision={0} />;
  }
  if (field.type === "decimal") {
    return (
      <InputNumber
        {...common}
        value={value as number | undefined}
        onChange={onChange}
        precision={field.scale ?? 2}
      />
    );
  }
  if (field.type === "json") {
    return <Input.TextArea rows={3} value={value as string | undefined} onChange={(event) => onChange?.(event.target.value)} />;
  }
  return <Input value={value as string | undefined} onChange={(event) => onChange?.(event.target.value)} />;
}

function RelationSelect({
  field,
  models,
  appId,
  value,
  onChange,
}: {
  field: FieldDef;
  models: ModelDef[];
  appId: string;
  value?: unknown;
  onChange?: (value: unknown) => void;
}) {
  const [options, setOptions] = useState<{ label: string; value: string }[]>([]);
  useEffect(() => {
    const target = models.find((model) => model.key === field.target_model);
    if (!target) return;
    let cancelled = false;
    api
      .queryRecords(appId, target.key, { limit: 300 })
      .then((result) => {
        if (!cancelled) {
          setOptions(result.items.map((item) => ({ label: item.title, value: item.id })));
        }
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [appId, models, field.target_model]);

  return (
    <Select
      style={{ width: "100%" }}
      mode={field.multiple ? "multiple" : undefined}
      value={value as string | string[] | undefined}
      onChange={onChange}
      options={options}
      allowClear
      placeholder={`选择${field.name}`}
      showSearch
      optionFilterProp="label"
    />
  );
}



export function RecordFormModal({
  open,
  appId,
  model,
  models,
  record,
  onClose,
  onSaved,
}: {
  open: boolean;
  appId: string;
  model: ModelDef;
  models: ModelDef[];
  record: RecordItem | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [form] = Form.useForm();
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (!open) return;
    const initial: Record<string, unknown> = {};
    for (const field of filterModelFields(model)) {
      initial[field.key] = toFormValue(field, record?.data[field.key]);
    }
    form.setFieldsValue(initial);
  }, [open, model, record, form]);

  const submit = async () => {
    try {
      const values = await form.validateFields();
      const payload: Record<string, unknown> = {};
      for (const field of filterModelFields(model)) {
        const converted = fromFormValue(field, values[field.key]);
        if (converted !== undefined) payload[field.key] = converted;
      }
      setSaving(true);
      if (record) {
        await api.updateRecord(appId, model.key, record.id, payload, record.revision);
      } else {
        await api.createRecord(appId, model.key, payload);
      }
      onSaved();
    } catch (err) {
      if (err instanceof Error && err.name !== "ValidationError") {
        message.error(err.message);
      }
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal
      open={open}
      title={`${record ? "编辑" : "新增"}${model.name}`}
      onCancel={onClose}
      onOk={submit}
      confirmLoading={saving}
      okText="保存"
      cancelText="取消"
      destroyOnClose
      width={520}
    >
      <Form form={form} layout="vertical">
        {filterModelFields(model).map((field) => (
          <Form.Item
            key={field.key}
            name={field.key}
            label={field.name}
            rules={[{ required: field.required, message: `请填写${field.name}` }]}
            extra={field.description ?? undefined}
          >
            <FieldInput field={field} models={models} appId={appId} />
          </Form.Item>
        ))}
      </Form>
    </Modal>
  );
}

export function ActionRunModal({
  appId,
  action,
  models,
  onClose,
  onDone,
}: {
  appId: string;
  action: ActionDef;
  models: ModelDef[];
  onClose: () => void;
  onDone: () => void;
}) {
  const [form] = Form.useForm();
  const [saving, setSaving] = useState(false);

  const submit = async () => {
    try {
      const values = await form.validateFields();
      const payload: Record<string, unknown> = {};
      for (const field of action.inputs) {
        const converted = fromFormValue(field, values[field.key]);
        if (converted !== undefined) payload[field.key] = converted;
      }
      setSaving(true);
      const result = await api.runAction(appId, action.key, payload);
      const effectCount = result.effects?.length ?? 0;
      message.success(`已完成，影响 ${effectCount} 条数据`);
      onDone();
    } catch (err) {
      if (err instanceof ApiError && err.code === "action_failed") {
        // The action already rolled back; the backend handed the failure to the
        // primary assistant, whose diagnosis lands in the conversations.
        const details = err.details as
          | { diagnosis?: { hint?: string } }
          | undefined;
        message.error({
          content:
            `${err.message}\n` +
            (details?.diagnosis?.hint ??
              "已交给主助手排查，结论会出现在对话里。"),
          duration: 8,
        });
      } else if (err instanceof Error && err.name !== "ValidationError") {
        message.error(err.message);
      }
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal
      open
      title={action.name}
      onCancel={onClose}
      onOk={submit}
      confirmLoading={saving}
      okText="执行"
      cancelText="取消"
      destroyOnClose
      width={480}
    >
      {action.description ? (
        <Typography.Paragraph type="secondary">{action.description}</Typography.Paragraph>
      ) : null}
      <Form form={form} layout="vertical">
        {action.inputs.map((field) => (
          <Form.Item
            key={field.key}
            name={field.key}
            label={field.name}
            rules={[{ required: field.required, message: `请填写${field.name}` }]}
          >
            <FieldInput field={field} models={models} appId={appId} />
          </Form.Item>
        ))}
      </Form>
    </Modal>
  );
}

function DetailView({ appId, component, model }: ComponentProps & { model: ModelDef }) {
  const recordId = component.props?.record_id as string | undefined;
  const [record, setRecord] = useState<RecordItem | null>(null);

  useEffect(() => {
    if (!recordId) return;
    let cancelled = false;
    api
      .queryRecords(appId, model.key, { filters: [{ field: "id", operator: "eq", value: recordId }] })
      .then((result) => {
        if (!cancelled) setRecord(result.items[0] ?? null);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [appId, model.key, recordId]);

  if (!recordId) {
    return (
      <Card size="small" title={component.title ?? model.name}>
        <Empty description="DetailView 需要在 props.record_id 中指定记录" image={Empty.PRESENTED_IMAGE_SIMPLE} />
      </Card>
    );
  }
  if (!record) return <Card size="small" loading />;

  const fields = component.fields
    ? model.fields.filter((field) => component.fields?.includes(field.key))
    : filterModelFields(model);

  return (
    <Card size="small" title={component.title ?? record.title}>
      <Space direction="vertical" size={6} style={{ width: "100%" }}>
        {fields.map((field) => (
          <Row key={field.key}>
            <Col span={8}>
              <Typography.Text type="secondary">{field.name}</Typography.Text>
            </Col>
            <Col span={16}>
              <ValueCell field={field} record={record} />
            </Col>
          </Row>
        ))}
      </Space>
    </Card>
  );
}

function InlineFormCard({
  appId,
  component,
  model,
  models,
  onChanged,
}: ComponentProps & { model: ModelDef }) {
  const [open, setOpen] = useState(false);
  return (
    <Card
      size="small"
      title={component.title ?? `${model.name}录入`}
      extra={
        <Button type="primary" icon={<PlusOutlined />} onClick={() => setOpen(true)}>
          填写
        </Button>
      }
    >
      {component.description ? (
        <Typography.Text type="secondary">{component.description}</Typography.Text>
      ) : (
        <Empty description="点击「填写」打开录入表单" image={Empty.PRESENTED_IMAGE_SIMPLE} />
      )}
      <RecordFormModal
        open={open}
        appId={appId}
        model={model}
        models={models}
        record={null}
        onClose={() => setOpen(false)}
        onSaved={() => {
          setOpen(false);
          message.success("已保存");
          onChanged();
        }}
      />
    </Card>
  );
}

function CalendarCard({ appId, component, model }: ComponentProps & { model: ModelDef }) {
  const dateField = (component.props?.date_field as string) ?? firstDateField(model)?.key;
  const titleField = (component.props?.title_field as string) ?? model.title_field;
  const [selected, setSelected] = useState<Dayjs>(dayjs());
  const [items, setItems] = useState<RecordItem[]>([]);

  useEffect(() => {
    if (!dateField) return;
    let cancelled = false;
    api
      .queryRecords(appId, model.key, {
        sort: [{ field: dateField, direction: "asc" }],
        limit: 500,
      })
      .then((result) => {
        if (!cancelled) setItems(result.items);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [appId, model.key, dateField]);

  if (!dateField) {
    return (
      <Alert type="warning" showIcon message={`${model.name} 没有日期字段，无法使用日历`} />
    );
  }

  const dayKey = selected.format("YYYY-MM-DD");
  const dayItems = items.filter((item) => String(item.data[dateField] ?? "").startsWith(dayKey));

  return (
    <Card size="small" title={component.title ?? "日历"}>
      <Row gutter={16}>
        <Col xs={24} md={14}>
          <Calendar
            fullscreen={false}
            value={selected}
            onSelect={setSelected}
            fullCellRender={(date) => {
              const key = date.format("YYYY-MM-DD");
              const count = items.filter((item) => String(item.data[dateField] ?? "").startsWith(key)).length;
              return (
                <div className="calendar-cell">
                  <span>{date.date()}</span>
                  {count ? <span className="calendar-dot">{count}</span> : null}
                </div>
              );
            }}
          />
        </Col>
        <Col xs={24} md={10}>
          <Typography.Text strong>{dayKey}</Typography.Text>
          <Space direction="vertical" size={4} style={{ width: "100%", marginTop: 8 }}>
            {dayItems.length === 0 ? (
              <Typography.Text type="secondary">当天没有记录</Typography.Text>
            ) : (
              dayItems.map((item) => (
                <Card key={item.id} size="small">
                  {String(item.data[titleField] ?? item.title)}
                </Card>
              ))
            )}
          </Space>
        </Col>
      </Row>
    </Card>
  );
}

function TabsCard(props: ComponentProps) {
  const tabs = (props.component.props?.tabs as { title: string; components: UIComponent[] }[]) ?? [];
  if (!tabs.length) return <Alert type="warning" showIcon message="Tabs 组件缺少 props.tabs" />;
  return (
    <Tabs
      items={tabs.map((tab, index) => ({
        key: String(index),
        label: tab.title,
        children: (
          <Renderer
            appId={props.appId}
            models={props.models}
            actions={props.actions}
            components={tab.components}
            refreshToken={props.refreshToken}
            onChanged={props.onChanged}
          />
        ),
      }))}
    />
  );
}

function FilterPanel({ component, onSearch }: ComponentProps) {
  const [value, setValue] = useState("");
  return (
    <Card size="small" title={component.title ?? "筛选"}>
      <Space>
        <Input
          allowClear
          prefix={<SearchOutlined />}
          placeholder="输入关键词筛选下方表格"
          style={{ width: 260 }}
          value={value}
          onChange={(event) => setValue(event.target.value)}
          onPressEnter={() => onSearch(value)}
        />
        <Button type="primary" onClick={() => onSearch(value)}>
          筛选
        </Button>
        <Button
          onClick={() => {
            setValue("");
            onSearch("");
          }}
        >
          重置
        </Button>
      </Space>
    </Card>
  );
}

function ActionButtons({ appId, component, actions, models, onChanged }: ComponentProps) {
  const [running, setRunning] = useState<string | null>(null);
  const action = actions.find((item) => item.key === running);

  return (
    <Space wrap>
      {(component.actions ?? []).map((ref) => (
        <Button
          key={ref.label}
          type={ref.kind === "create" ? "primary" : "default"}
          onClick={() => {
            if (ref.kind === "run_action" && ref.action) setRunning(ref.action);
          }}
        >
          {ref.label}
        </Button>
      ))}
      {action ? (
        <ActionRunModal
          appId={appId}
          action={action}
          models={models}
          onClose={() => setRunning(null)}
          onDone={() => {
            setRunning(null);
            onChanged();
          }}
        />
      ) : null}
    </Space>
  );
}
