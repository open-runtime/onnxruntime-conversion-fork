﻿// Copyright (c) Microsoft Corporation. All rights reserved.
// Licensed under the MIT License.

using Microsoft.ML.OnnxRuntime.Tensors;
using System;
using System.Runtime.InteropServices;
using System.Text;
using System.Collections.Generic;
using System.Linq;

namespace Microsoft.ML.OnnxRuntime
{
    /// <summary>
    /// This helper class contains methods to create native OrtValue from a managed value object
    /// </summary>
    internal static class NativeOnnxValueHelper
    {
        /// <summary>
        /// Converts C# UTF-16 string to UTF-8 zero terminated
        /// byte[] instance
        /// </summary>
        /// <param name="s">string to be converted</param>
        /// <returns>UTF-8 encoded equivalent</returns>
        internal static byte[] StringToZeroTerminatedUtf8(string s)
        {
            int arraySize = UTF8Encoding.UTF8.GetByteCount(s);
            byte[] utf8Bytes = new byte[arraySize + 1];
            if (arraySize != UTF8Encoding.UTF8.GetBytes(s, 0, s.Length, utf8Bytes, 0))
            {
                throw new OnnxRuntimeException(ErrorCode.RuntimeException, "Failed to convert to UTF8");
            }
            utf8Bytes[utf8Bytes.Length - 1] = 0;
            return utf8Bytes;
        }

        /// <summary>
        /// Reads UTF-8 encode string from a C zero terminated string
        /// and converts it into a C# UTF-16 encoded string
        /// </summary>
        /// <param name="nativeUtf8">pointer to native or pinned memory where Utf-8 resides</param>
        /// <returns></returns>
        internal static string StringFromNativeUtf8(IntPtr nativeUtf8)
        {
            // .NET 8.0 has Marshal.PtrToStringUTF8 that does the below
            int len = 0;
            while (Marshal.ReadByte(nativeUtf8, len) != 0) ++len;
            byte[] buffer = new byte[len];
            Marshal.Copy(nativeUtf8, buffer, 0, len);
            return Encoding.UTF8.GetString(buffer, 0, buffer.Length);
        }

        /// <summary>
        /// Reads UTF-8 string from native C zero terminated string,
        /// converts it to C# UTF-16 string and returns both C# string and utf-8
        /// bytes as a zero terminated array, suitable for use as a C-string
        /// </summary>
        /// <param name="nativeUtf8">input</param>
        /// <param name="str">C# UTF-16 string</param>
        /// <param name="utf8">UTF-8 bytes in a managed buffer, zero terminated</param>
        internal static void StringAndUtf8FromNative(IntPtr nativeUtf8, out string str, out byte[] utf8)
        {
            // .NET 8.0 has Marshal.PtrToStringUTF8 that does the below
            int len = 0;
            while (Marshal.ReadByte(nativeUtf8, len) != 0) ++len;
            utf8 = new byte[len + 1];
            Marshal.Copy(nativeUtf8, utf8, 0, len);
            utf8[len] = 0;
            str = Encoding.UTF8.GetString(utf8, 0, len);
        }

        internal static string StringFromUtf8Span(ReadOnlySpan<byte> utf8Span)
        {
            // XXX: For now we have to copy into byte[], this produces a copy
            // Converting from span is available in later versions
            var utf8Bytes = utf8Span.ToArray();
            return Encoding.UTF8.GetString(utf8Bytes, 0, utf8Bytes.Length);
        }

        /// <summary>
        /// Run helper
        /// </summary>
        /// <param name="names">names to convert to zero terminated utf8 and pin</param>
        /// <param name="extractor">delegate for string extraction from inputs</param>
        /// <param name="cleanupList">list to add pinned memory to for later disposal</param>
        /// <returns></returns>
        internal static IntPtr[] ConvertNamesToUtf8<T>(IReadOnlyCollection<T> names, NameExtractor<T> extractor,
            DisposableList<IDisposable> cleanupList)
        {
            cleanupList.Capacity += names.Count;
            var result = new IntPtr[names.Count];
            for (int i = 0; i < names.Count; ++i)
            {
                var name = extractor(names.ElementAt(i));
                var utf8Name = NativeOnnxValueHelper.StringToZeroTerminatedUtf8(name);
                var pinnedHandle = new Memory<byte>(utf8Name).Pin();
                cleanupList.Add(pinnedHandle);
                unsafe
                {
                    result[i] = (IntPtr)pinnedHandle.Pointer;
                }
            }
            return result;
        }

        /// <summary>
        /// Converts C# UTF-16 string to UTF-8 zero terminated
        /// byte[] instance
        /// </summary>
        /// <param name="str">string to be converted</param>
        /// <returns>UTF-8 encoded equivalent</returns>
        internal static byte[] GetPlatformSerializedString(string str)
        {
            if (RuntimeInformation.IsOSPlatform(OSPlatform.Windows))
                return System.Text.Encoding.Unicode.GetBytes(str + Char.MinValue);
            else
                return StringToZeroTerminatedUtf8(str);
        }

        // Delegate for string extraction from an arbitrary input/output object
        internal delegate string NameExtractor<in TInput>(TInput input);
    }

    internal static class TensorElementTypeConverter
    {
        public static bool GetTypeAndWidth(TensorElementType elemType, out Type type, out int width)
        {
            bool result = true;
            TensorElementTypeInfo typeInfo = TensorBase.GetElementTypeInfo(elemType);
            if (typeInfo != null)
            {
                type = typeInfo.TensorType;
                width = typeInfo.TypeSize;
            }
            else
            {
                type = null;
                width = 0;
                result = false;
            }
            return result;
        }
    }
}
