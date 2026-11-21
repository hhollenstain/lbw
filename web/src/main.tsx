import { type ReactNode } from "react";
import { createRoot } from "react-dom/client";

import { ClaudeLinkSettings } from "./sections/integrations/ClaudeLinkSettings";
import { IntegrationsSection } from "./sections/integrations/IntegrationsSection";
import { RunnersSection } from "./sections/integrations/RunnersSection";
import { ModelsSection } from "./sections/models/ModelsSection";
import { ProjectModelsSection } from "./sections/models/ProjectModelsSection";
import { OrgRolesSection } from "./sections/organization/OrgRolesSection";
import { OrgSkillsSection } from "./sections/organization/OrgSkillsSection";
import { RolesSection } from "./sections/roles/RolesSection";
import { SkillsSection } from "./sections/skills/SkillsSection";
import { PricesSection } from "./sections/usage/PricesSection";
import { UsageDashboard } from "./sections/usage/UsageDashboard";

function mount(id: string, node: ReactNode) {
  const host = document.getElementById(id);
  if (host) createRoot(host).render(node);
}

mount("org-models-root", <ModelsSection />);
mount("org-skills-root", <OrgSkillsSection />);
mount("org-roles", <OrgRolesSection />);
mount("project-skills-root", <SkillsSection />);
mount("workspace-roles", <RolesSection />);
mount("workspace-models", <ProjectModelsSection />);
mount("project-usage-root", <UsageDashboard scope="project" />);
mount("personal-usage-root", <UsageDashboard scope="me" />);
mount("org-usage-root", <UsageDashboard scope="org" />);
mount("org-prices-root", <PricesSection />);
mount("personal-integrations-root", <IntegrationsSection />);
mount("personal-runners-root", <RunnersSection />);
mount("org-claude-link-root", <ClaudeLinkSettings />);
