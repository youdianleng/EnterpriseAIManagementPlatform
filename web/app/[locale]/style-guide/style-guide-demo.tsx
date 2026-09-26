"use client";

import { useState } from "react";

import type { Locale } from "@/lib/i18n/config";
import type { Dictionary } from "@/lib/i18n/dictionaries";
import { formatDecimal } from "@/lib/format";
import { Alert } from "@/lib/ui/alert";
import { Button } from "@/lib/ui/button";
import { Dialog } from "@/lib/ui/dialog";
import { SelectField, TextField } from "@/lib/ui/field";
import { DataTable, type Column } from "@/lib/ui/table";

type Hours = { id: string; person: string; department: string; hours: number };

const SAMPLE_ROWS: Hours[] = [
  { id: "1", person: "Ana Martín", department: "Recursos Humanos", hours: 37.5 },
  { id: "2", person: "Luis Fernández", department: "Finanzas", hours: 11.5 },
  { id: "3", person: "Marta Ibáñez", department: "Operaciones", hours: 1.5 },
];

/** Values chosen so the decimal separator is visible at a glance. */
const TABULAR_SAMPLES = [1.5, 11.5, 111.5, 1111.5];

/** Interactive half of the style guide: everything that needs state. */
export function StyleGuideDemo({ dict, locale }: { dict: Dictionary; locale: Locale }) {
  const t = dict.styleGuide;
  const [name, setName] = useState("");
  const [email, setEmail] = useState("not-an-email");
  const [department, setDepartment] = useState("");
  const [dialogOpen, setDialogOpen] = useState(false);

  const columns: Array<Column<Hours>> = [
    { key: "person", header: t.table.employee, render: (row) => row.person },
    { key: "department", header: t.table.department, render: (row) => row.department },
    {
      key: "hours",
      header: t.table.hours,
      numeric: true,
      render: (row) => formatDecimal(row.hours, locale),
    },
  ];

  return (
    <div className="flex flex-col gap-8">
      <section aria-labelledby="sg-states">
        <h2 id="sg-states" className="text-lg font-semibold">
          {t.states.title}
        </h2>
        <p className="mt-1 mb-4 text-fg-muted">{t.states.description}</p>
        <div className="flex flex-col gap-3">
          <Alert tone="info">{t.states.info}</Alert>
          <Alert tone="success">{t.states.success}</Alert>
          <Alert tone="warning">{t.states.warning}</Alert>
          <Alert tone="danger" role="alert">
            {t.states.danger}
          </Alert>
          <Alert tone="neutral">{t.states.neutral}</Alert>
        </div>
      </section>

      <section aria-labelledby="sg-buttons">
        <h2 id="sg-buttons" className="text-lg font-semibold">
          {t.buttons.title}
        </h2>
        <p className="mt-1 mb-4 text-fg-muted">{t.buttons.description}</p>
        <div className="flex flex-wrap items-center gap-3">
          <Button variant="primary">{t.buttons.primary}</Button>
          <Button variant="secondary">{t.buttons.secondary}</Button>
          <Button variant="ghost">{t.buttons.ghost}</Button>
          <Button variant="danger">{t.buttons.danger}</Button>
          <Button variant="secondary" size="sm">
            {t.buttons.small}
          </Button>
          <Button variant="primary" disabled>
            {t.buttons.disabled}
          </Button>
        </div>
      </section>

      <section aria-labelledby="sg-forms">
        <h2 id="sg-forms" className="text-lg font-semibold">
          {t.forms.title}
        </h2>
        <p className="mt-1 mb-4 text-fg-muted">{t.forms.description}</p>
        <form
          className="grid max-w-2xl gap-4 sm:grid-cols-2"
          onSubmit={(event) => event.preventDefault()}
        >
          <TextField
            label={t.forms.nameLabel}
            placeholder={t.forms.namePlaceholder}
            hint={t.forms.nameHint}
            value={name}
            onChange={setName}
            required
          />
          <TextField
            label={t.forms.emailLabel}
            type="email"
            value={email}
            onChange={setEmail}
            error={t.forms.emailError}
          />
          <SelectField
            label={t.forms.departmentLabel}
            placeholder={t.forms.departmentPlaceholder}
            value={department}
            onChange={setDepartment}
            options={[
              { value: "hr", label: t.forms.departmentOption1 },
              { value: "finance", label: t.forms.departmentOption2 },
            ]}
          />
          <div className="flex items-end">
            <Button type="submit">{t.forms.submit}</Button>
          </div>
        </form>
      </section>

      <section aria-labelledby="sg-table">
        <h2 id="sg-table" className="text-lg font-semibold">
          {t.table.title}
        </h2>
        <p className="mt-1 mb-4 text-fg-muted">{t.table.description}</p>
        <DataTable
          columns={columns}
          rows={SAMPLE_ROWS}
          getRowKey={(row) => row.id}
          caption={t.table.caption}
          emptyMessage={t.table.empty}
        />
      </section>

      <section aria-labelledby="sg-dialog">
        <h2 id="sg-dialog" className="text-lg font-semibold">
          {t.dialog.title}
        </h2>
        <p className="mt-1 mb-4 text-fg-muted">{t.dialog.description}</p>
        <Button variant="secondary" onClick={() => setDialogOpen(true)}>
          {t.dialog.open}
        </Button>
        <Dialog
          open={dialogOpen}
          onClose={() => setDialogOpen(false)}
          title={t.dialog.title}
          closeLabel={t.dialog.close}
          footer={
            <>
              <Button variant="secondary" onClick={() => setDialogOpen(false)}>
                {t.dialog.cancel}
              </Button>
              <Button variant="primary" onClick={() => setDialogOpen(false)}>
                {t.dialog.confirm}
              </Button>
            </>
          }
        >
          {t.dialog.body}
        </Dialog>
      </section>
    </div>
  );
}
