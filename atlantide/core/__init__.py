"""atlantide.core: dependency-free SDK surface (types, Resource, Provider ABC).

Fallible lookups/checks return ``returns.result.Result``; the aliases below are
re-exported so downstream modules import one vocabulary from here.
"""

from returns.pipeline import is_successful
from returns.result import Failure, Result, Success

from atlantide.core.component import Component, child
from atlantide.core.context import Context
from atlantide.core.errors import (
    ArtifactError,
    AtlantideError,
    ComponentError,
    CycleError,
    FuelExhaustedError,
    IRError,
    LanguageError,
    LockError,
    NonDeterministicError,
    PolicyViolationError,
    PreventDestroyError,
    ProviderError,
    RegistryError,
    SecretsError,
    StackOutputCycleError,
    StateError,
)
from atlantide.core.fields import (
    Mutability,
    computed,
    field_mutability,
    immutable,
    is_sensitive,
    mutable,
    physical_name_field,
    secret,
    sensitive_fields,
)
from atlantide.core.inline import inline_stack_outputs
from atlantide.core.lifecycle import Lifecycle
from atlantide.core.policy import PolicyBinding, PolicyLevel
from atlantide.core.provider import Provider
from atlantide.core.registry import ProviderRegistry, check_compatible, parse_semver
from atlantide.core.resource import (
    Resource,
    ResourceRegistry,
    active_registry,
    collecting,
    output,
)
from atlantide.core.stack import (
    DEFAULT_STACK,
    Stack,
    StackReference,
    current_stack,
    current_stack_name_prefix,
    current_stack_region,
    current_stack_tags,
    region,
)
from atlantide.core.types import (
    UNSET,
    Input,
    Ref,
    SecretRef,
    StackOutputRef,
    Transform,
    concat,
    interpolate,
    join,
)

__all__ = [
    "DEFAULT_STACK",
    "UNSET",
    "ArtifactError",
    "AtlantideError",
    "Component",
    "ComponentError",
    "Context",
    "CycleError",
    "Failure",
    "FuelExhaustedError",
    "IRError",
    "Input",
    "LanguageError",
    "Lifecycle",
    "LockError",
    "Mutability",
    "NonDeterministicError",
    "PolicyBinding",
    "PolicyLevel",
    "PolicyViolationError",
    "PreventDestroyError",
    "Provider",
    "ProviderError",
    "ProviderRegistry",
    "Ref",
    "RegistryError",
    "Resource",
    "ResourceRegistry",
    "Result",
    "SecretRef",
    "SecretsError",
    "Stack",
    "StackOutputCycleError",
    "StackOutputRef",
    "StackReference",
    "StateError",
    "Success",
    "Transform",
    "active_registry",
    "check_compatible",
    "child",
    "collecting",
    "computed",
    "concat",
    "current_stack",
    "current_stack_name_prefix",
    "current_stack_region",
    "current_stack_tags",
    "field_mutability",
    "immutable",
    "inline_stack_outputs",
    "interpolate",
    "is_sensitive",
    "is_successful",
    "join",
    "mutable",
    "output",
    "parse_semver",
    "physical_name_field",
    "region",
    "secret",
    "sensitive_fields",
]
