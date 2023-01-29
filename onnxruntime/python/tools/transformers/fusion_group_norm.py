# -------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation.  All rights reserved.
# Licensed under the MIT License.
# --------------------------------------------------------------------------
from logging import getLogger
from typing import Dict

import numpy as np
from fusion_base import Fusion
from fusion_utils import FusionUtils
from onnx import TensorProto, helper
from onnx_model import OnnxModel

logger = getLogger(__name__)


class FusionGroupNorm(Fusion):
    def __init__(self, model: OnnxModel):
        super().__init__(model, "GroupNorm", "Add")

    def fuse(self, add_node, input_name_to_nodes: Dict, output_name_to_node: Dict):
        """
         Fuse Group Normalization subgraph into one node GroupNorm.
         The following is the pattern with swish activation:
               +----------------Shape-------------------------------+
               |                                                    |
               |    (0, 32, -1)                                     v     (512x1x1) (512x1x1) (optional)
           [Root] --> Reshape -------> InstanceNormalization --> Reshape ---> Mul --> Add --> Mul--> [output]
        Bx512xHxW                 (scale=ones(32), B=zeros(32))                        |       ^     Bx512xHxW
                                                                                       |       |
                                                                                       +--->Sigmoid (optional)
        The Mul and Sigmoid before output is for Swish activation. They are optional.
        """
        nodes = self.model.match_parent_path(
            add_node, ["Mul", "Reshape", "InstanceNormalization", "Reshape"], [0, 0, 0, 0], output_name_to_node
        )
        if nodes is None:
            return

        weight_mul, reshape_4d, instance_norm, reshape_3d = nodes
        root = reshape_3d.input[0]

        parents = self.model.match_parent_path(reshape_4d, ["Shape"], [1], output_name_to_node)
        if parents is None:
            return
        if parents[0].input[0] != root:
            return
        shape_node = parents[0]

        # Check whether it has swish activation.
        swish_mul = self.model.find_first_child_by_type(add_node, "Mul")
        swish_sigmoid = None
        if swish_mul is not None:
            sigmoid_path = self.model.match_parent_path(swish_mul, ["Sigmoid"], [None], output_name_to_node)
            if sigmoid_path is not None:
                swish_sigmoid = sigmoid_path[0]

        weight_input = weight_mul.input[1 - self.model.input_index(reshape_4d.output[0], weight_mul)]
        if not self.model.is_constant_with_specified_dimension(weight_input, 3, "group norm weight"):
            return

        bias_input = add_node.input[1 - self.model.input_index(weight_mul.output[0], add_node)]
        if not self.model.is_constant_with_specified_dimension(bias_input, 3, "layernorm bias"):
            return

        weight = self.model.get_constant_value(weight_input)
        if not (len(weight.shape) == 3 and weight.shape[1] == 1 and weight.shape[2] == 1):
            return

        bias = self.model.get_constant_value(bias_input)
        if not (len(bias.shape) == 3 and bias.shape[1] == 1 and bias.shape[2] == 1):
            return

        weight_elements = np.prod(weight.shape)
        bias_elements = np.prod(bias.shape)
        if weight_elements != bias_elements:
            return

        instance_norm_scale = self.model.get_constant_value(instance_norm.input[1])
        if instance_norm_scale is None:
            return
        instance_norm_bias = self.model.get_constant_value(instance_norm.input[2])
        if instance_norm_bias is None:
            return

        if not (
            len(instance_norm_scale.shape) == 1
            and len(instance_norm_bias.shape) == 1
            and instance_norm_scale.shape == instance_norm_bias.shape
            and instance_norm_scale.shape[0] == 32
        ):
            logger.info(f"InstanceNormalization groups={instance_norm_scale.shape[0]}")
            return

        if not np.allclose(np.ones_like(instance_norm_scale), instance_norm_scale):
            return
        if not np.allclose(np.zeros_like(instance_norm_bias), instance_norm_bias):
            return

        group_norm_name = self.model.create_node_name("GroupNorm", name_prefix="GroupNorm")

        if weight_elements not in [320, 640, 960, 1280, 1920, 2560] + [128, 256, 512]:
            logger.info(f"GroupNorm channels={weight_elements}")

        gamma = helper.make_tensor(
            name=group_norm_name + "_gamma",
            data_type=TensorProto.FLOAT,
            dims=[weight_elements],
            vals=weight.flatten().tolist(),
        )
        self.model.add_initializer(gamma, self.this_graph_name)

        beta = helper.make_tensor(
            name=group_norm_name + "_beta",
            data_type=TensorProto.FLOAT,
            dims=[bias_elements],
            vals=bias.flatten().tolist(),
        )
        self.model.add_initializer(beta, self.this_graph_name)

        last_node = add_node
        subgraph_nodes = [add_node, weight_mul, reshape_4d, instance_norm, reshape_3d, shape_node]
        has_swish_activation = swish_mul and swish_sigmoid
        if swish_mul and swish_sigmoid:
            subgraph_nodes.extend([swish_mul, swish_sigmoid])
            last_node = swish_mul

        if not self.model.is_safe_to_fuse_nodes(
            subgraph_nodes,
            last_node.output,
            input_name_to_nodes,
            output_name_to_node,
        ):
            self.nodes_to_remove.extend([last_node])
        else:
            self.nodes_to_remove.extend(subgraph_nodes)

        # instance_norm_scale might from Constant node. Use prune graph to clear it.
        self.prune_graph = True

        # Right now GroupNorm only support float16 input. Need add a Cast in fp32 model.
        utils = FusionUtils(self.model)

        input = root
        output = last_node.output[0]
        if weight.dtype == np.float32:
            # Add a Cast node to get float16 input for GroupNorm
            cast_input, _cast_node = utils.cast_input(root, "float16")
            input = cast_input

            # Add a Cast node to convert back to float32 after GroupNorm
            output = group_norm_name + "_out"
            cast_node = helper.make_node("Cast", inputs=[group_norm_name + "_out"], outputs=[last_node.output[0]])
            cast_node.attribute.extend([helper.make_attribute("to", int(TensorProto.FLOAT))])
            self.model.add_node(cast_node)

        # NCHW to NHWC
        transpose_input = helper.make_node(
            "Transpose",
            [input],
            [input + "_NHWC"],
            name=self.model.create_node_name("Transpose", name_prefix="Transpose_NCHW_to_NHWC"),
            perm=[0, 2, 3, 1],
        )

        new_node = helper.make_node(
            "GroupNorm",
            inputs=[input + "_NHWC", group_norm_name + "_gamma", group_norm_name + "_beta"],
            outputs=[output + "_NHWC"],
            name=group_norm_name,
        )

        new_node.attribute.extend(instance_norm.attribute)
        new_node.attribute.extend([helper.make_attribute("groups", 32)])
        new_node.attribute.extend([helper.make_attribute("activation", 1 if has_swish_activation else 0)])
        new_node.domain = "com.microsoft"

        # NHWC to NCHW
        transpose_output = helper.make_node(
            "Transpose",
            [output + "_NHWC"],
            [output],
            name=self.model.create_node_name("Transpose", name_prefix="Transpose_NHWC_to_NCHW"),
            perm=[0, 3, 1, 2],
        )

        self.nodes_to_add.append(new_node)
        self.nodes_to_add.append(transpose_input)
        self.nodes_to_add.append(transpose_output)

        self.node_name_to_graph_name[new_node.name] = self.this_graph_name
        self.node_name_to_graph_name[transpose_input.name] = self.this_graph_name
        self.node_name_to_graph_name[transpose_output.name] = self.this_graph_name
        return True
