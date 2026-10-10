!> Kind parameters shared by the demo modules.
module demo_kinds
    use, intrinsic :: iso_fortran_env, only: real64, int64
    implicit none
    private
    public :: wp, ip
    integer, parameter :: wp = real64  !! working precision
    integer, parameter :: ip = int64
end module demo_kinds
