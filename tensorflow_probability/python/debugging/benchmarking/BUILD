# Copyright 2018 The TensorFlow Probability Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================
# Build rules for TensorFlow Probability benchmarking framework.

package(
    default_visibility = [
        "//tensorflow_probability:__subpackages__",
    ],
)

licenses(["notice"])

py_library(
    name = "benchmarking",
    srcs = ["__init__.py"],
    srcs_version = "PY3",
    deps = [
        ":benchmark_tf_function",
    ],
)

py_library(
    name = "benchmark_tf_function",
    srcs = ["benchmark_tf_function.py"],
    srcs_version = "PY3",
    deps = [
        # tensorflow dep,
    ],
)
