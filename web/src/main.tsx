import { type ReactNode } from "react";
import { createRoot } from "react-dom/client";

import { ModelsSection } from "./sections/models/ModelsSection";
import { ProjectModelsSection } from "./sections/models/ProjectModelsSection";
import { OrgRolesSection } from "./sections/organization/OrgRolesSection";
import { OrgSkillsSection } from "./sections/organization/OrgSkillsSection";
import { RolesSection } from "./sections/roles/RolesSection";
import { SkillsSection } from "./sections/skills/SkillsSection";

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
