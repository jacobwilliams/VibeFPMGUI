!> Compiles normally; with the "broken" feature (macro DEMO_BROKEN) it has a
!> syntax error, so you can see a failed compile in the build grid.
module demo_broken
    implicit none
    private
    public :: broken_answer

contains

    integer function broken_answer()
#ifdef DEMO_BROKEN
        broken_answer = this is not valid fortran
#else
        broken_answer = 42
#endif
    end function broken_answer

end module demo_broken
