import unittest

from mcp_controller_builder.templates import (
    MANY_TO_MANY,
    Entity,
    EntityField,
    render_integration_test,
    render_orm,
    render_controller,
    render_schemas,
    render_sql_repository,
)


class RepositoryTemplateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.role = Entity(
            name="Role",
            plural="roles",
            fields=[EntityField(name="name", type="str")],
        )
        self.member = Entity(
            name="Member",
            fields=[
                EntityField(
                    name="username",
                    type="str",
                    min_length=1,
                    max_length=200,
                ),
                EntityField(
                    name="phone",
                    type="str",
                    required=False,
                    max_length=30,
                ),
                EntityField(
                    name="role_ids",
                    type="relation",
                    related_entity="Role",
                    relation=MANY_TO_MANY,
                ),
            ],
        )
        self.known = {"Member": self.member, "Role": self.role}

    def test_orm_uses_project_base_and_declares_related_model_type(self) -> None:
        source = render_orm(self.member, self.known)

        self.assertIn(
            "from shared.infrastructure.persistence.database import Base",
            source,
        )
        self.assertIn("if TYPE_CHECKING:", source)
        self.assertIn(
            "from infrastructure.database.models.role_orm import RoleORM",
            source,
        )
        self.assertIn(
            'roles: Mapped[list["RoleORM"]] = '
            'relationship(secondary=member_roles, lazy="selectin")',
            source,
        )
        compile(source, "member_orm.py", "exec")

    def test_repository_initializes_base_and_persists_m2m_on_create_and_update(
        self,
    ) -> None:
        source = render_sql_repository(self.member, self.known)

        self.assertIn(
            'super().__init__(db, Member, MemberORM, "Member")',
            source,
        )
        self.assertIn("def _get_active_roles(self, role_ids: list[UUID])", source)
        self.assertIn("orm_entity.roles = self._get_active_roles(entity.role_ids)", source)
        self.assertIn("def update(self, entity_id: int | UUID, entity: Member)", source)
        self.assertIn("orm_entity.username = entity.username", source)
        self.assertIn("orm_entity.phone = entity.phone", source)
        self.assertIn("orm_entity.roles = self._get_active_roles(entity.role_ids)", source)
        self.assertIn("orm_entity.updated_on = datetime.now(UTC)", source)
        compile(source, "member_repository.py", "exec")

    def test_repository_initializes_base_without_relations(self) -> None:
        product = Entity(
            name="Product",
            fields=[EntityField(name="name", type="str")],
        )

        source = render_sql_repository(product)

        self.assertIn(
            'super().__init__(db, Product, ProductORM, "Product")',
            source,
        )
        compile(source, "product_repository.py", "exec")

    def test_schemas_keep_constrained_optional_fields_optional(self) -> None:
        source = render_schemas(self.member)

        self.assertIn(
            "phone: str | None = Field(default=None, max_length=30)",
            source,
        )
        compile(source, "member_schemas.py", "exec")

    def test_generated_integration_test_covers_m2m_create_and_update(self) -> None:
        source = render_integration_test(self.member, self.known)

        self.assertIn('client.post("/api/v1/roles/", json={"name": "ejemplo"})', source)
        self.assertIn("def test_member_role_ids_persist_on_create_and_update", source)
        self.assertIn('created.json()["role_ids"] == [first_role_id]', source)
        self.assertIn('updated.json()["role_ids"] == [second_role_id]', source)
        compile(source, "test_members_api.py", "exec")

    def test_generated_patch_schema_controller_and_integration_test(self) -> None:
        schemas = render_schemas(self.member)
        controller = render_controller(self.member)
        integration_test = render_integration_test(self.member, self.known)

        self.assertIn(
            "class MemberPatch(BaseModel):",
            schemas,
        )
        self.assertIn('username: str = Field(default="", min_length=1, max_length=200)', schemas)
        self.assertIn("phone: str | None = Field(default=None, max_length=30)", schemas)
        self.assertIn("role_ids: list[UUID] = Field(default_factory=list)", schemas)
        self.assertIn("def patch_member(", controller)
        self.assertIn("patch_schema=MemberPatch", controller)
        self.assertIn("patch_factory=patch_member", controller)
        self.assertIn("def test_patch_member_preserves_unprovided_fields", integration_test)
        self.assertIn('json={"username": "x"}', integration_test)
        compile(schemas, "member_schemas.py", "exec")
        compile(controller, "member_controller.py", "exec")
        compile(integration_test, "test_members_api.py", "exec")


if __name__ == "__main__":
    unittest.main()
