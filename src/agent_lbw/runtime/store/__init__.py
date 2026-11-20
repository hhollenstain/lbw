"""Directory database. One module per table group.

`schema` creates the tables. `crypto` seals task goals and the GitHub App
private key. `install` is the local account. `teams`, `duties`, and `projects`
are access. `tasks` stores a request, and `schedules` stores when one should
run. `rules` indexes a rule whose text lives in that scope's KiwiFS space.
`github` stores the app id and installation. `assignments` records which
skills and enforced agent-team tasks an organization applies to projects.
`skill_repos` indexes a cloned skill repository. Its files and commit live in
a KiwiFS space named from the organization, team, and project ids.
`catalog` stores bring-your-own endpoints, the model names they returned,
and the agents that may build with a chosen model. A grant on a project, a
team, or a person is what allows use.
Model context stays in the run directory.
"""
