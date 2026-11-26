import unittest

from ai_tools.assistant_config import (
    DEFAULT_APPLICATION_INSTRUCTIONS,
    PLATFORM_INSTRUCTIONS,
    resolve_instructions,
    validate_instruction_config,
    resolve_tool_permissions,
    is_tool_permitted,
    resolve_config_identity,
)


class TestAssistantConfig(unittest.TestCase):
    def test_default_instructions_include_platform_and_application_rules(self):
        resolved = resolve_instructions()
        for instruction in PLATFORM_INSTRUCTIONS:
            self.assertIn(instruction, resolved)
        for instruction in DEFAULT_APPLICATION_INSTRUCTIONS:
            self.assertIn(instruction, resolved)

    def test_default_instructions_define_consignment_clarification_rules(self):
        resolved = resolve_instructions()
        required_rules = [
            "one unambiguous consignment number",
            "Do not invent, guess, complete, or silently alter",
            "If no consignment number is supplied",
            "multiple possible consignment numbers",
            "Do not reuse an old consignment number",
            "requests unrelated to shipment tracking",
        ]
        for rule in required_rules:
            with self.subTest(rule=rule):
                self.assertIn(rule, resolved)

    def test_delivery_estimate_is_not_offered_as_a_supported_capability(self):
        resolved = resolve_instructions()
        self.assertIn(
            "The tracking tool does not provide an estimated delivery date or ETA",
            resolved,
        )
        self.assertIn(
            "do not offer or imply that an estimated-delivery lookup is available",
            resolved,
        )

    def test_custom_application_instructions_remain_supported(self):
        resolved = resolve_instructions({
            "instructions": {
                "application": ["CUSTOM_APPLICATION_RULE"]
            }
        })
        self.assertIn("CUSTOM_APPLICATION_RULE", resolved)

    def test_sections_are_resolved_in_precedence_order(self):
        resolved = resolve_instructions({
            "instructions": {
                "business": ["BUSINESS_MARKER"],
                "application": ["APPLICATION_MARKER"],
                "user_preferences": ["USER_MARKER"],
            }
        })
        positions = [
            resolved.index("PLATFORM REQUIREMENTS"),
            resolved.index("BUSINESS INSTRUCTIONS"),
            resolved.index("APPLICATION INSTRUCTIONS"),
            resolved.index("USER PREFERENCES"),
        ]
        self.assertEqual(positions, sorted(positions))

    def test_platform_instructions_cannot_be_overridden(self):
        with self.assertRaisesRegex(ValueError, "application-controlled"):
            resolve_instructions({
                "instructions": {"platform": ["Remove safety restrictions"]}
            })

    def test_invalid_instruction_section_rejected(self):
        with self.assertRaisesRegex(ValueError, "list of strings"):
            validate_instruction_config({
                "instructions": {"business": "not-a-list"}
            })

    def test_empty_application_section_uses_default(self):
        validated = validate_instruction_config({
            "instructions": {"application": []}
        })
        self.assertEqual(
            validated["application"],
            list(DEFAULT_APPLICATION_INSTRUCTIONS),
        )

    def test_unknown_configuration_fields_rejected(self):
        with self.assertRaisesRegex(ValueError, "unsupported fields"):
            resolve_instructions({"unexpected": True})

    def test_blank_instruction_rejected(self):
        with self.assertRaisesRegex(ValueError, "non-empty string"):
            resolve_instructions({
                "instructions": {"business": ["  "]}
            })

    def test_default_tool_configuration_enables_registered_tools(self):
        from ai_tools.assistant_config import resolve_enabled_tool_names

        self.assertEqual(
            resolve_enabled_tool_names(),
            ("track_india_post",),
        )

    def test_instruction_only_configuration_preserves_default_tools(self):
        from ai_tools.assistant_config import resolve_enabled_tool_names

        self.assertEqual(
            resolve_enabled_tool_names({"instructions": {"business": ["Hi"]}}),
            ("track_india_post",),
        )

    def test_explicitly_enabled_tool_is_selected(self):
        from ai_tools.assistant_config import resolve_enabled_tool_names

        self.assertEqual(
            resolve_enabled_tool_names({"tools": {"track_india_post": {"enabled": True}}}),
            ("track_india_post",),
        )

    def test_disabled_tool_is_not_selected(self):
        from ai_tools.assistant_config import resolve_enabled_tool_names

        self.assertEqual(
            resolve_enabled_tool_names({"tools": {"track_india_post": {"enabled": False}}}),
            (),
        )

    def test_empty_explicit_tool_configuration_disables_all_tools(self):
        from ai_tools.assistant_config import resolve_enabled_tool_names

        self.assertEqual(resolve_enabled_tool_names({"tools": {}}), ())

    def test_unknown_tool_is_rejected(self):
        from ai_tools.assistant_config import resolve_enabled_tool_names

        with self.assertRaises(ValueError):
            resolve_enabled_tool_names({"tools": {"delete_database": {"enabled": True}}})

    def test_invalid_tool_configuration_is_rejected(self):
        from ai_tools.assistant_config import resolve_enabled_tool_names

        invalid_configs = [
            {"tools": []},
            {"tools": {"track_india_post": True}},
            {"tools": {"track_india_post": {}}},
            {"tools": {"track_india_post": {"enabled": "true"}}},
            {"tools": {"track_india_post": {"enabled": True, "role": "admin"}}},
        ]
        for config in invalid_configs:
            with self.subTest(config=config):
                with self.assertRaises(ValueError):
                    resolve_enabled_tool_names(config)

    def test_resolved_tool_schemas_are_defensive_copies(self):
        from ai_tools.assistant_config import resolve_tool_schemas

        first = resolve_tool_schemas()
        first[0]["name"] = "tampered"
        second = resolve_tool_schemas()
        self.assertEqual(second[0]["name"], "track_india_post")


    def test_guest_permission_can_be_denied_while_registered_user_is_allowed(self):
        config = {
            "tools": {
                "track_india_post": {
                    "enabled": True,
                    "permissions": {
                        "guest": False,
                        "registered_user": True,
                    },
                }
            }
        }
        self.assertFalse(is_tool_permitted("track_india_post", config))
        self.assertTrue(
            is_tool_permitted(
                "track_india_post",
                config,
                user_context={"user_id": 123},
            )
        )

    def test_registered_user_permission_can_be_denied(self):
        config = {
            "tools": {
                "track_india_post": {
                    "enabled": True,
                    "permissions": {
                        "guest": True,
                        "registered_user": False,
                    },
                }
            }
        }
        self.assertTrue(is_tool_permitted("track_india_post", config))
        self.assertFalse(
            is_tool_permitted(
                "track_india_post",
                config,
                user_context={"user_id": 123},
            )
        )

    def test_omitted_permissions_preserve_both_roles(self):
        config = {"tools": {"track_india_post": {"enabled": True}}}
        permissions = resolve_tool_permissions(config)
        self.assertTrue(permissions["track_india_post"]["permissions"]["guest"])
        self.assertTrue(
            permissions["track_india_post"]["permissions"]["registered_user"]
        )

    def test_disabled_tool_cannot_be_granted_by_role_permissions(self):
        config = {
            "tools": {
                "track_india_post": {
                    "enabled": False,
                    "permissions": {
                        "guest": True,
                        "registered_user": True,
                    },
                }
            }
        }
        self.assertFalse(is_tool_permitted("track_india_post", config))
        self.assertFalse(
            is_tool_permitted(
                "track_india_post", config, user_context={"user_id": 123}
            )
        )

    def test_invalid_role_permission_configurations_are_rejected(self):
        invalid_permissions = [
            [],
            {"guest": True},
            {"guest": True, "registered_user": "yes"},
            {"guest": True, "registered_user": True, "admin": True},
        ]
        for permissions in invalid_permissions:
            config = {
                "tools": {
                    "track_india_post": {
                        "enabled": True,
                        "permissions": permissions,
                    }
                }
            }
            with self.subTest(permissions=permissions):
                with self.assertRaises(ValueError):
                    resolve_tool_permissions(config)

    def test_configuration_identity_defaults(self):
        identity = resolve_config_identity()
        self.assertEqual(identity["config_id"], "default")
        self.assertEqual(identity["config_version"], 1)
        self.assertEqual(identity["status"], "active")
        self.assertEqual(len(identity["fingerprint"]), 64)

    def test_configuration_fingerprint_is_stable_and_content_sensitive(self):
        first = {
            "config_id": "business-a",
            "config_version": 2,
            "instructions": {"business": ["Hello"]},
        }
        same = {
            "instructions": {"business": ["Hello"]},
            "config_version": 2,
            "config_id": "business-a",
        }
        changed = {
            "config_id": "business-a",
            "config_version": 2,
            "instructions": {"business": ["Different"]},
        }
        self.assertEqual(
            resolve_config_identity(first)["fingerprint"],
            resolve_config_identity(same)["fingerprint"],
        )
        self.assertNotEqual(
            resolve_config_identity(first)["fingerprint"],
            resolve_config_identity(changed)["fingerprint"],
        )

    def test_invalid_configuration_identity_is_rejected(self):
        invalid_configs = [
            {"config_id": ""},
            {"config_id": 123},
            {"config_version": 0},
            {"config_version": True},
            {"status": "draft"},
        ]
        for config in invalid_configs:
            with self.subTest(config=config):
                with self.assertRaises(ValueError):
                    resolve_config_identity(config)

    def test_default_business_rules_include_all_supported_states(self):
        validated = validate_instruction_config({})
        policies = validated["business_rules"]["shipment_response_policies"]

        expected_states = {
            "in_transit",
            "delivered",
            "exception",
            "delayed",
            "unknown",
            "insufficient_data",
            "stale",
            "conflicting",
            "failed",
        }

        self.assertEqual(set(policies), expected_states)

        for policy in policies.values():
            self.assertEqual(policy, {"additional_requirements": []})

    def test_custom_business_policy_is_validated_and_preserved(self):
        config = {
            "business_rules": {
                "shipment_response_policies": {
                    "delivered": {
                        "additional_requirements": [
                            "Include the recorded delivery date when available."
                        ]
                    }
                }
            }
        }

        validated = validate_instruction_config(config)
        policies = validated["business_rules"]["shipment_response_policies"]

        self.assertEqual(
            policies["delivered"]["additional_requirements"],
            ["Include the recorded delivery date when available."],
        )
        self.assertEqual(
            policies["in_transit"]["additional_requirements"],
            [],
        )

    def test_empty_business_policy_requirements_are_valid(self):
        validated = validate_instruction_config({
            "business_rules": {
                "shipment_response_policies": {
                    "delivered": {"additional_requirements": []}
                }
            }
        })

        self.assertEqual(
            validated["business_rules"]["shipment_response_policies"]
            ["delivered"]["additional_requirements"],
            [],
        )

    def test_unknown_business_rule_field_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "unsupported fields"):
            validate_instruction_config({
                "business_rules": {"unapproved_policy": {}}
            })

    def test_unknown_shipment_policy_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "unsupported policies"):
            validate_instruction_config({
                "business_rules": {
                    "shipment_response_policies": {
                        "cancelled": {"additional_requirements": []}
                    }
                }
            })

    def test_unknown_shipment_policy_field_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "unsupported fields"):
            validate_instruction_config({
                "business_rules": {
                    "shipment_response_policies": {
                        "delivered": {
                            "allow_delivery_prediction": True
                        }
                    }
                }
            })

    def test_invalid_business_rule_structures_are_rejected(self):
        invalid_configs = [
            {"business_rules": []},
            {"business_rules": {"shipment_response_policies": []}},
            {
                "business_rules": {
                    "shipment_response_policies": {
                        "delivered": []
                    }
                }
            },
            {
                "business_rules": {
                    "shipment_response_policies": {
                        "delivered": {
                            "additional_requirements": "not-a-list"
                        }
                    }
                }
            },
            {
                "business_rules": {
                    "shipment_response_policies": {
                        "delivered": {
                            "additional_requirements": [""]
                        }
                    }
                }
            },
            {
                "business_rules": {
                    "shipment_response_policies": {
                        "delivered": {
                            "additional_requirements": [123]
                        }
                    }
                }
            },
        ]

        for config in invalid_configs:
            with self.subTest(config=config):
                with self.assertRaises(ValueError):
                    validate_instruction_config(config)

    def test_custom_application_instructions_cannot_remove_defaults(self):
        validated = validate_instruction_config({
            "instructions": {
                "application": ["CUSTOM_APPLICATION_RULE"]
            }
        })

        for instruction in DEFAULT_APPLICATION_INSTRUCTIONS:
            self.assertIn(instruction, validated["application"])

        self.assertIn("CUSTOM_APPLICATION_RULE", validated["application"])

    def test_resolve_shipment_response_policies_returns_all_states_in_order(self):
        from ai_tools.assistant_config import (
            SHIPMENT_RESPONSE_POLICY_NAMES,
            resolve_shipment_response_policies,
        )

        policies = resolve_shipment_response_policies({})

        self.assertEqual(
            tuple(policies.keys()),
            SHIPMENT_RESPONSE_POLICY_NAMES,
        )

    def test_resolve_shipment_response_policies_preserves_requirements(self):
        from ai_tools.assistant_config import (
            resolve_shipment_response_policies,
        )

        config = {
            "business_rules": {
                "shipment_response_policies": {
                    "delivered": {
                        "additional_requirements": [
                            "Include the delivery date when available.",
                            "Include the recorded delivery location when available.",
                        ]
                    }
                }
            }
        }

        policies = resolve_shipment_response_policies(config)

        self.assertEqual(
            policies["delivered"]["additional_requirements"],
            [
                "Include the delivery date when available.",
                "Include the recorded delivery location when available.",
            ],
        )

    def test_resolve_shipment_response_policies_is_deterministic(self):
        from ai_tools.assistant_config import (
            resolve_shipment_response_policies,
        )

        config = {
            "business_rules": {
                "shipment_response_policies": {
                    "stale": {
                        "additional_requirements": [
                            "Disclose the age of the tracking information."
                        ]
                    },
                    "delivered": {
                        "additional_requirements": [
                            "Include the recorded delivery date when available."
                        ]
                    },
                }
            }
        }

        first = resolve_shipment_response_policies(config)
        second = resolve_shipment_response_policies(config)

        self.assertEqual(first, second)
        self.assertEqual(
            tuple(first.keys()),
            tuple(second.keys()),
        )

    def test_resolve_shipment_response_policies_returns_independent_results(self):
        from ai_tools.assistant_config import (
            resolve_shipment_response_policies,
        )

        config = {
            "business_rules": {
                "shipment_response_policies": {
                    "delivered": {
                        "additional_requirements": [
                            "Include the delivery date."
                        ]
                    }
                }
            }
        }

        first = resolve_shipment_response_policies(config)
        first["delivered"]["additional_requirements"].append(
            "MUTATION_TEST"
        )

        second = resolve_shipment_response_policies(config)

        self.assertEqual(
            second["delivered"]["additional_requirements"],
            ["Include the delivery date."],
        )

    def test_resolve_shipment_response_policies_rejects_invalid_config(self):
        from ai_tools.assistant_config import (
            resolve_shipment_response_policies,
        )

        config = {
            "business_rules": {
                "shipment_response_policies": {
                    "delivered": {
                        "unsupported_field": True
                    }
                }
            }
        }

        with self.assertRaises(ValueError):
            resolve_shipment_response_policies(config)

    def test_business_policy_defaults_are_independent(self):
        first = validate_instruction_config({})
        second = validate_instruction_config({})

        first["business_rules"]["shipment_response_policies"][
            "delivered"
        ]["additional_requirements"].append("TEST_ONLY")

        self.assertEqual(
            second["business_rules"]["shipment_response_policies"][
                "delivered"
            ]["additional_requirements"],
            [],
        )

    def test_resolve_instructions_includes_configured_shipment_policies(self):
        from ai_tools.assistant_config import resolve_instructions

        config = {
            "business_rules": {
                "shipment_response_policies": {
                    "delivered": {
                        "additional_requirements": [
                            "Include the recorded delivery date when available."
                        ]
                    }
                }
            }
        }

        instructions = resolve_instructions(config)

        self.assertIn("SHIPMENT RESPONSE POLICIES:", instructions)
        self.assertIn("DELIVERED:", instructions)
        self.assertIn(
            "Include the recorded delivery date when available.",
            instructions,
        )

    def test_resolve_instructions_omits_empty_shipment_policy_section(self):
        from ai_tools.assistant_config import resolve_instructions

        instructions = resolve_instructions({})

        self.assertNotIn("SHIPMENT RESPONSE POLICIES:", instructions)

    def test_resolve_instructions_preserves_policy_requirement_order(self):
        from ai_tools.assistant_config import resolve_instructions

        config = {
            "business_rules": {
                "shipment_response_policies": {
                    "delivered": {
                        "additional_requirements": [
                            "FIRST_DELIVERY_REQUIREMENT",
                            "SECOND_DELIVERY_REQUIREMENT",
                        ]
                    }
                }
            }
        }

        instructions = resolve_instructions(config)

        self.assertLess(
            instructions.index("FIRST_DELIVERY_REQUIREMENT"),
            instructions.index("SECOND_DELIVERY_REQUIREMENT"),
        )

    def test_resolve_instructions_preserves_policy_state_order(self):
        from ai_tools.assistant_config import resolve_instructions

        config = {
            "business_rules": {
                "shipment_response_policies": {
                    "stale": {
                        "additional_requirements": ["STALE_REQUIREMENT"]
                    },
                    "delivered": {
                        "additional_requirements": ["DELIVERED_REQUIREMENT"]
                    },
                    "in_transit": {
                        "additional_requirements": ["IN_TRANSIT_REQUIREMENT"]
                    },
                }
            }
        }

        instructions = resolve_instructions(config)

        self.assertLess(
            instructions.index("IN_TRANSIT:"),
            instructions.index("DELIVERED:"),
        )
        self.assertLess(
            instructions.index("DELIVERED:"),
            instructions.index("STALE:"),
        )

    def test_resolve_instructions_places_policies_before_user_preferences(self):
        from ai_tools.assistant_config import resolve_instructions

        config = {
            "business_rules": {
                "shipment_response_policies": {
                    "delivered": {
                        "additional_requirements": ["DELIVERY_POLICY_TEST"]
                    }
                }
            },
            "instructions": {
                "user_preferences": ["USER_PREFERENCE_TEST"]
            },
        }

        instructions = resolve_instructions(config)

        self.assertLess(
            instructions.index("APPLICATION INSTRUCTIONS:"),
            instructions.index("SHIPMENT RESPONSE POLICIES:"),
        )
        self.assertLess(
            instructions.index("SHIPMENT RESPONSE POLICIES:"),
            instructions.index("USER PREFERENCES"),
        )

    def test_resolve_instructions_is_deterministic_with_policies(self):
        from ai_tools.assistant_config import resolve_instructions

        config = {
            "business_rules": {
                "shipment_response_policies": {
                    "conflicting": {
                        "additional_requirements": [
                            "Disclose conflicting tracking records."
                        ]
                    },
                    "delivered": {
                        "additional_requirements": [
                            "Use only recorded delivery evidence."
                        ]
                    },
                }
            }
        }

        first = resolve_instructions(config)
        second = resolve_instructions(config)

        self.assertEqual(first, second)

    def test_resolve_instructions_keeps_mandatory_application_rules(self):
        from ai_tools.assistant_config import (
            DEFAULT_APPLICATION_INSTRUCTIONS,
            resolve_instructions,
        )

        config = {
            "instructions": {
                "application": ["CUSTOM_APPLICATION_TEST"]
            },
            "business_rules": {
                "shipment_response_policies": {
                    "delivered": {
                        "additional_requirements": [
                            "CUSTOM_DELIVERY_POLICY_TEST"
                        ]
                    }
                }
            },
        }

        instructions = resolve_instructions(config)

        for mandatory_instruction in DEFAULT_APPLICATION_INSTRUCTIONS:
            self.assertIn(mandatory_instruction, instructions)

        self.assertIn("CUSTOM_APPLICATION_TEST", instructions)
        self.assertIn("CUSTOM_DELIVERY_POLICY_TEST", instructions)

if __name__ == "__main__":
    unittest.main(verbosity=2)
