from libc.stdint cimport intptr_t

cdef intptr_t get_current_stream_ptr()
cdef intptr_t get_stream_ptr(int device_id)
cdef set_current_stream_ptr(intptr_t ptr, int device_id=*)
cpdef intptr_t get_default_stream_ptr()
cdef bint is_ptds_enabled()
cdef void set_current_cublas_workspace(
        intptr_t ptr, size_t size, int device_id=*)
cpdef (intptr_t, size_t) get_current_cublas_workspace(int device_id=*)
